### 0.3.2
 - Docker build fix because of?
 https://stackoverflow.com/questions/75608323/how-do-i-solve-error-externally-managed-environment-every-time-i-use-pip-3

### 0.3.1
 - add async-timeout to build, needed?
 - do not print passwords..
 - password in config.json
### 0.3.0
 - now handling the messages from home assistant
 - Fixed Random disconnects from home assistant
   (Let me,know if it worked)
### 0.2.2
- add init to config.json, is this working?
## 0.4.0 (Optimized Fork by goodlucknow)
- Complete refactoring for improved reliability and efficiency
- Fixed critical event loop bug (prevents crashes)
- Reduced Kodi ping interval from 100s to 30s (3x faster disconnect detection)
- Implemented exponential backoff for reconnection (2s-60s)
- Real-time message handling (only last button press on reconnect)
- Added queue overflow protection
- PEP 8 compliance, docstrings, and type hints
- Better error handling and logging
see https://github.com/goodlucknow/kodi2home for more info

---
## Original DJJo14 versions below:

### 0.2.1
- reconnect if home assistant cuts the connection as workaround. <br>
see https://github.com/DJJo14/kodi2home for more info and examples
## 0.2
- reconect if home assistant connection fails

## 0.1
- First working verion 
