@echo off
rem Hermes Gateway Watchdog - hidden launcher (console-free).
rem Task Scheduler runs this .cmd; `start /min ""` spawns pythonw detached,
rem the batch itself exits in <1s so no window lingers.
start /min "" "C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe" "C:\Users\Ha Trung\AppData\Local\hermes\scripts\gateway-watchdog.py"
