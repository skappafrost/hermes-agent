@echo off
setlocal
set "PATH=C:\Users\Ha Trung\AppData\Local\hermes\node;C:\Users\Ha Trung\AppData\Roaming\npm;C:\Users\Ha Trung\.local\bin;%PATH%"
set "AGENTMEMORY_URL=http://127.0.0.1:3111"
"C:\Users\Ha Trung\AppData\Local\hermes\node\node.exe" "C:\Users\Ha Trung\AppData\Roaming\npm\node_modules\@agentmemory\agentmemory\dist\standalone.mjs" %*
