@echo off
rem AgentMemory engine autostart (iii engine + worker, REST 127.0.0.1:3111).
rem Self-healing: if the worker exits unexpectedly, wait 30s and restart it
rem (a detached iii engine is adopted, not re-spawned).
rem Permanent stop: create file C:\Users\Ha Trung\.agentmemory\STOP then run
rem   agentmemory stop   (delete the STOP file to re-enable autostart).
setlocal
set "PATH=C:\Users\Ha Trung\AppData\Local\hermes\node;C:\Users\Ha Trung\AppData\Roaming\npm;C:\Users\Ha Trung\.local\bin;%PATH%"
set "AGENTMEMORY_URL=http://127.0.0.1:3111"
cd /d "C:\Users\Ha Trung\AppData\Roaming\npm\node_modules\@agentmemory\agentmemory"
:loop
if exist "C:\Users\Ha Trung\.agentmemory\STOP" exit /b 0
"C:\Users\Ha Trung\AppData\Local\hermes\node\node.exe" dist\cli.mjs
if exist "C:\Users\Ha Trung\.agentmemory\STOP" exit /b 0
timeout /t 30 /nobreak >nul
goto loop
