@echo off
rem Install/refresh the agentmemory Hermes memory-provider for all named profiles.
rem One canonical copy lives at %ROOT%\plugins\agentmemory (used by the default profile).
rem Each named profile gets a directory JUNCTION to that same source, so there is a single
rem file to edit. Re-run this after creating a new profile. Idempotent: existing links are
rem left alone. Junctions need no admin. Never place the plugin inside the hermes-agent
rem checkout (hermes update stashes/cleans untracked files there).
setlocal
set "ROOT=C:\Users\Ha Trung\AppData\Local\hermes"
set "SRC=%ROOT%\plugins\agentmemory"
if not exist "%SRC%\__init__.py" (
  echo [ERROR] canonical provider not found at "%SRC%" 1>&2
  exit /b 1
)
for %%P in (neo_agent nexus_agent vex_agent zen_agent) do call :link "%%P"
echo [ok] agentmemory provider linked for all named profiles
exit /b 0

:link
set "PLUGINS=%ROOT%\profiles\%~1\plugins"
if not exist "%PLUGINS%" mkdir "%PLUGINS%"
if exist "%PLUGINS%\agentmemory" (
  echo [skip] %~1 already has agentmemory
  goto :eof
)
mklink /J "%PLUGINS%\agentmemory" "%SRC%" >nul
if errorlevel 1 (
  echo [WARN] junction failed for %~1; falling back to copy 1>&2
  xcopy /E /I /Q "%SRC%" "%PLUGINS%\agentmemory" >nul
) else (
  echo [ok] %~1 -> junction
)
goto :eof
