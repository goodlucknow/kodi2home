"""
Kodi2Home - Bridge between Kodi and Home Assistant

This application connects Kodi and Home Assistant via WebSockets,
allowing Kodi remote control buttons to trigger Home Assistant automations.
"""

import asyncio
import functools
import json
import logging
import signal
import sys
from typing import Dict, Any, Optional

import websockets
from pykodi import get_kodi_connection, Kodi, CannotConnectError, InvalidAuthError

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout
)

# Example keymap entry for Kodi:
# <volume_up>NotifyAll("kodi2home", "kodi_call_home", {"trigger":"automation.volume_up"})</volume_up>


class Kodi2Home:
    """
    Bridge between Kodi and Home Assistant.

    Listens for NotifyAll messages from Kodi and triggers
    Home Assistant automations via WebSocket API.
    """

    # Default configuration values
    DEFAULT_QUEUE_SIZE = 20
    DEFAULT_KODI_PING_INTERVAL = 30  # seconds (reduced from 100 for faster detection)
    DEFAULT_RETRY_MIN_DELAY = 2  # seconds
    DEFAULT_RETRY_MAX_DELAY = 60  # seconds
    DEFAULT_MAX_RETRIES = None  # None = infinite retries

    def __init__(self, config_file: str):
        """
        Initialize Kodi2Home bridge.

        Args:
            config_file: Path to JSON configuration file
        """
        with open(config_file, "r") as inputfile:
            self.config = json.load(inputfile)

        # Message queue for Home Assistant commands
        self.queue = asyncio.Queue(maxsize=self.DEFAULT_QUEUE_SIZE)

        # WebSocket ID counter for Home Assistant API
        self.id_nr = 1

        # Connection objects
        self.kodi_connection = None
        self.kodi = None
        self.websocket = None

        # Shutdown flag
        self.shutdown_requested = False

        # Retry delay state
        self.ha_retry_delay = self.DEFAULT_RETRY_MIN_DELAY
        self.kodi_retry_delay = self.DEFAULT_RETRY_MIN_DELAY

    async def connect_to_kodi(self) -> bool:
        """
        Connect to Kodi WebSocket API.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            logging.info(
                f"Connecting to Kodi: {self.config['kodi_adress']}:"
                f"{self.config['kodi_http_port']}/{self.config['kodi_ws_port']}"
            )

            self.kodi_connection = get_kodi_connection(
                self.config["kodi_adress"],
                self.config["kodi_http_port"],
                self.config["kodi_ws_port"],
                self.config["kodi_username"],
                self.config["kodi_password"],
                False,  # SSL
                5,  # Timeout
                None,  # Session
            )

            await self.kodi_connection.connect()
            self.kodi = Kodi(self.kodi_connection)

            # Register callback for NotifyAll messages
            self.kodi_connection.server.Other.kodi_call_home = self.kodi_call_home

            # Get Kodi version info
            properties = await self.kodi.get_application_properties(["name", "version"])
            logging.info(f"Kodi connected: {properties}")

            # Reload keymaps so changes take effect without restarting Kodi
            await self.kodi_connection.server.Input.ExecuteAction("reloadkeymaps")
            logging.info("Kodi keymaps reloaded")

            # Reset retry delay on successful connection
            self.kodi_retry_delay = self.DEFAULT_RETRY_MIN_DELAY
            return True

        except CannotConnectError as e:
            logging.error(f"Cannot connect to Kodi: {e}")
            return False
        except InvalidAuthError as e:
            logging.error(f"Kodi authentication failed: {e}")
            raise  # Don't retry on auth errors
        except Exception as e:
            logging.error(f"Unexpected error connecting to Kodi: {e}")
            return False

    async def connect_to_home_assistant(self) -> bool:
        """
        Connect to Home Assistant WebSocket API with retry logic.

        Returns:
            True if connection successful, False otherwise
        """
        if self.shutdown_requested:
            return False

        try:
            logging.info(
                f"Connecting to Home Assistant: {self.config['home_adress']}"
            )

            # Configure SSL
            ssl_context = True if self.config["home_ssl"] else None

            # Connect to WebSocket
            self.websocket = await websockets.connect(
                self.config["home_adress"],
                ssl=ssl_context,
                ping_interval=30,  # Send ping every 30s
                ping_timeout=10,   # Wait 10s for pong
            )

            # Receive auth required message
            auth_required = await self.websocket.recv()
            logging.debug(f"Home Assistant: {auth_required}")

            # Send authentication
            auth_message = {
                "type": "auth",
                "access_token": sys.argv[1]
            }
            await self.websocket.send(json.dumps(auth_message))

            # Receive auth result
            auth_result = await self.websocket.recv()
            auth_data = json.loads(auth_result)

            if auth_data.get("type") == "auth_ok":
                logging.info("Home Assistant connected and authenticated")
                # Reset retry delay on successful connection
                self.ha_retry_delay = self.DEFAULT_RETRY_MIN_DELAY
                return True
            else:
                logging.error(f"Home Assistant authentication failed: {auth_result}")
                return False

        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 502:
                logging.warning("Home Assistant returned 502 (starting up or restarting)")
            else:
                logging.error(f"WebSocket error {e.status_code}: {e}")
            return False

        except websockets.exceptions.WebSocketException as e:
            logging.error(f"WebSocket connection error: {e}")
            return False

        except Exception as e:
            logging.error(f"Unexpected error connecting to Home Assistant: {e}")
            return False

    async def kodi_call_home(self, sender: str, data: Dict[str, Any]):
        """
        Callback invoked when Kodi sends NotifyAll message.

        Args:
            sender: Source of the notification
            data: Notification data containing 'trigger' key
        """
        if "trigger" not in data:
            logging.warning(f"Received notification without 'trigger' key: {data}")
            return

        self.id_nr += 1
        service_call = {
            "id": self.id_nr,
            "type": "call_service",
            "domain": "automation",
            "service": "trigger",
            "service_data": {
                "entity_id": data["trigger"],
            },
        }

        logging.info(f"Kodi trigger: {data['trigger']}")

        # Try to add to queue, handle overflow gracefully
        try:
            self.queue.put_nowait(service_call)
        except asyncio.QueueFull:
            logging.warning(
                f"Queue full! Dropping trigger: {data['trigger']}. "
                f"Consider increasing queue size or check connection health."
            )

    async def send_to_home_assistant(self):
        """
        Send queued messages to Home Assistant.

        Handles reconnection automatically. On reconnection, only sends the
        most recent button press to avoid flooding automations with stale commands.
        """
        while not self.shutdown_requested:
            try:
                # Get message from queue
                service_call = await self.queue.get()

                # Ensure we're connected
                if self.websocket is None or self.websocket.closed:
                    logging.warning("Home Assistant disconnected, reconnecting...")

                    # Keep only the last message (current one), drop older ones
                    last_message = service_call
                    dropped = 0
                    while not self.queue.empty():
                        try:
                            last_message = self.queue.get_nowait()
                            dropped += 1
                        except asyncio.QueueEmpty:
                            break

                    if dropped > 0:
                        logging.info(f"Dropped {dropped} old button press(es), keeping only the last")

                    # Attempt reconnection
                    if not await self._reconnect_home_assistant():
                        # Reconnection failed, drop the message (real-time behavior)
                        logging.warning(f"Reconnection failed, dropping trigger: {last_message['service_data']['entity_id']}")
                        continue

                    # Send only the last message as a "reconnection ping"
                    service_call = last_message

                # Send message
                await self.websocket.send(json.dumps(service_call))
                logging.debug(f"Sent to Home Assistant: {service_call}")

            except (websockets.exceptions.ConnectionClosedOK,
                    websockets.exceptions.ConnectionClosedError) as e:
                logging.warning(f"Home Assistant connection closed during send: {e}")

                # Drop the message (real-time behavior - don't retry stale button presses)
                logging.info(f"Dropping trigger due to disconnect: {service_call['service_data']['entity_id']}")

                # Reconnect for next message
                await self._reconnect_home_assistant()

            except Exception as e:
                logging.error(f"Unexpected error sending to Home Assistant: {e}")
                # Drop the message rather than retry
                logging.warning(f"Dropping trigger due to error: {service_call.get('service_data', {}).get('entity_id', 'unknown')}")

    async def receive_from_home_assistant(self):
        """
        Receive messages from Home Assistant.

        This loop keeps the WebSocket connection alive and consumes
        response messages to prevent buffer overflow.
        """
        while not self.shutdown_requested:
            try:
                # Ensure connection
                if self.websocket is None or self.websocket.closed:
                    logging.info("Connecting to Home Assistant for receive loop...")
                    if not await self._reconnect_home_assistant():
                        await asyncio.sleep(self.ha_retry_delay)
                        continue

                # Receive messages
                while not self.shutdown_requested:
                    try:
                        message = await asyncio.wait_for(
                            self.websocket.recv(),
                            timeout=60  # Timeout to check shutdown flag periodically
                        )

                        if message:
                            logging.debug(f"Home Assistant response: {message}")
                            # Process responses if needed (currently just logging)

                    except asyncio.TimeoutError:
                        # Normal timeout, loop continues
                        continue

            except websockets.exceptions.ConnectionClosedError as e:
                logging.warning(f"Home Assistant receive connection closed: {e}")
                await asyncio.sleep(self.ha_retry_delay)

            except Exception as e:
                logging.error(f"Error in Home Assistant receive loop: {e}")
                await asyncio.sleep(self.ha_retry_delay)

    async def monitor_kodi_connection(self):
        """
        Monitor Kodi connection with periodic pings.

        Reconnects automatically if connection is lost.
        """
        # Initial connection
        while not self.shutdown_requested:
            if await self.connect_to_kodi():
                break

            logging.info(f"Retrying Kodi connection in {self.kodi_retry_delay}s...")
            await asyncio.sleep(self.kodi_retry_delay)
            self.kodi_retry_delay = min(
                self.kodi_retry_delay * 2,
                self.DEFAULT_RETRY_MAX_DELAY
            )

        # Monitor connection
        while not self.shutdown_requested:
            try:
                if self.kodi_connection and self.kodi_connection.connected:
                    # Ping to check connection
                    await self.kodi.ping()
                    await asyncio.sleep(self.DEFAULT_KODI_PING_INTERVAL)
                else:
                    # Connection lost, reconnect
                    logging.warning("Kodi connection lost, reconnecting...")
                    await self._reconnect_kodi()

            except CannotConnectError:
                logging.warning("Kodi ping failed, reconnecting...")
                await self._reconnect_kodi()

            except InvalidAuthError:
                logging.error("Kodi authentication error - stopping monitor")
                return

            except Exception as e:
                logging.error(f"Error monitoring Kodi connection: {e}")
                await self._reconnect_kodi()

    async def _reconnect_kodi(self):
        """Reconnect to Kodi with exponential backoff."""
        while not self.shutdown_requested:
            logging.info(f"Reconnecting to Kodi in {self.kodi_retry_delay}s...")
            await asyncio.sleep(self.kodi_retry_delay)

            if await self.connect_to_kodi():
                logging.info("Kodi reconnected successfully")
                return

            # Exponential backoff
            self.kodi_retry_delay = min(
                self.kodi_retry_delay * 2,
                self.DEFAULT_RETRY_MAX_DELAY
            )

    async def _reconnect_home_assistant(self) -> bool:
        """
        Reconnect to Home Assistant with exponential backoff.

        Returns:
            True if reconnection successful, False otherwise
        """
        # Close existing connection if any
        if self.websocket and not self.websocket.closed:
            try:
                await self.websocket.close()
            except Exception:
                pass

        # Try to reconnect
        while not self.shutdown_requested:
            if await self.connect_to_home_assistant():
                logging.info("Home Assistant reconnected successfully")
                return True

            logging.info(f"Retrying Home Assistant connection in {self.ha_retry_delay}s...")
            await asyncio.sleep(self.ha_retry_delay)

            # Exponential backoff
            self.ha_retry_delay = min(
                self.ha_retry_delay * 2,
                self.DEFAULT_RETRY_MAX_DELAY
            )

        return False

    async def shutdown(self):
        """Gracefully shutdown all connections."""
        logging.info("Shutting down Kodi2Home...")
        self.shutdown_requested = True

        # For real-time remote control, we don't flush queued messages
        # Button presses are ephemeral - only current actions matter
        remaining = self.queue.qsize()
        if remaining > 0:
            logging.info(f"Discarding {remaining} queued button press(es) - shutdown in progress")

        # Close connections
        if self.websocket and not self.websocket.closed:
            try:
                await self.websocket.close()
                logging.info("Home Assistant connection closed")
            except Exception as e:
                logging.error(f"Error closing Home Assistant connection: {e}")

        if self.kodi_connection and self.kodi_connection.connected:
            try:
                await self.kodi_connection.close()
                logging.info("Kodi connection closed")
            except Exception as e:
                logging.error(f"Error closing Kodi connection: {e}")

        logging.info("Shutdown complete")


def ask_exit(signame: str, k2h: Kodi2Home):
    """Signal handler for graceful shutdown."""
    logging.info(f"Received signal {signame}, initiating shutdown...")
    asyncio.create_task(k2h.shutdown())


async def setup_signal_handlers(k2h: Kodi2Home):
    """Setup signal handlers for graceful shutdown."""
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(
            getattr(signal, signame),
            functools.partial(ask_exit, signame, k2h)
        )


async def async_main():
    """Main async entry point."""
    k2h = Kodi2Home("options.json")

    try:
        # Setup signal handlers
        await setup_signal_handlers(k2h)

        # Run all tasks concurrently
        await asyncio.gather(
            k2h.monitor_kodi_connection(),
            k2h.send_to_home_assistant(),
            k2h.receive_from_home_assistant(),
            return_exceptions=True  # Don't stop all tasks if one fails
        )

    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
    except Exception as e:
        logging.error(f"Unexpected error in main loop: {e}")
    finally:
        await k2h.shutdown()


def main():
    """Main entry point."""
    try:
        # Use asyncio.run() for proper event loop management
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logging.info("Application stopped")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()


# References:
# https://github.com/lijinqiu1/homeassistant/blob/master/websocketAPI.py
# https://developers.home-assistant.io/docs/api/websocket#calling-a-service
# https://forums.homeseer.com/forum/media-plug-ins/media-discussion/kodi-xbmc-spud/76271-how-to-trigger-hs-events-from-kodi-xbmc-interface
# https://www.home-assistant.io/integrations/kodi/
# https://github.com/home-assistant/core/blob/dev/homeassistant/components/kodi/media_player.py
# https://github.com/OnFreund/PyKodi/blob/master/pykodi/kodi.py
# https://github.com/emlove/jsonrpc-websocket
# https://kodi.wiki/view/Keymap#Add-on_built-in.27s
# https://github.com/xbmc/xbmc/blob/master/system/keymaps/keyboard.xml
# https://github.com/home-assistant/core/blob/dev/homeassistant/components/kodi/__init__.py
