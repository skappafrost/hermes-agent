"""Enable GRAPH_EXTRACTION_ENABLED in ~/.agentmemory/.env and restart the engine worker.

The engine caches .env at process start (loadEnvFile() memoises), so a worker restart is required.
agentmemory-engine.cmd self-heals: it respawns the worker ~30s after exit and adopts the running
iii engine, which re-reads .env — so we only kill the node worker, never iii.exe, never the gateway.
Never prints secret values, only key names.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ENV = Path.home() / ".agentmemory" / ".env"
B = "http://127.0.0.1:3111"
KEY = "GRAPH_EXTRACTION_ENABLED"

lines = ENV.read_text(encoding="utf-8").splitlines()
if any(l.strip().startswith(f"{KEY}=") for l in lines):
    lines = [l if not l.strip().startswith(f"{KEY}=") else f"{KEY}=true" for l in lines]
    action = "value set to true"
else:
    lines = lines + ["", f"{KEY}=true  # LLM entity/relation extraction (Graph tab only; not recall)"]
    action = "key appended"
ENV.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
names = [l.split("=", 1)[0].strip() for l in ENV.read_text(encoding="utf-8").splitlines()
         if l.strip() and not l.strip().startswith("#") and "=" in l]
print(f".env: {action}. keys now = {names}")

def cmdline_of(target):
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={target}').CommandLine"],
        capture_output=True, text=True).stdout.strip()


def find_workers():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
         "Where-Object { $_.CommandLine -match 'cli\\.mjs' } | ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.strip().isdigit()]


pid = int((Path.home() / ".agentmemory" / "worker.pid").read_text().strip())
info = cmdline_of(pid)
if not info:  # stale pid file: fall back to a cmdline scan, and only act on a single match
    found = find_workers()
    if len(found) != 1:
        print(f"worker.pid is stale and the scan found {found} node workers — refusing to guess")
        sys.exit(1)
    pid, info = found[0], cmdline_of(found[0])
engine_pids = {(Path.home() / ".agentmemory" / "iii.pid").read_text().strip()}
if "cli.mjs" not in info.lower() or "node.exe" not in info.lower():
    print(f"REFUSING to kill pid {pid}: cmdline is {info[:140]!r} — not the node worker")
    sys.exit(1)
if str(pid) in engine_pids:
    print(f"REFUSING: pid {pid} is the iii engine, not the worker")
    sys.exit(1)
print(f"worker pid={pid} cmdline={info[:90]!r} (engine pid {engine_pids} untouched) -> stopping")
subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"], check=False)


def flags():
    try:
        with urllib.request.urlopen(B + "/agentmemory/config/flags", timeout=6) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return {f["key"]: f["enabled"] for f in d.get("flags", [])}, d
    except Exception:
        return None, None


deadline = time.time() + 180
seen_false = False
while time.time() < deadline:
    time.sleep(6)
    st, _ = flags()
    if st is None:
        continue
    if not st.get(KEY):
        seen_false = True          # old worker still coming back up
        continue
    print(f"respawned worker reports {KEY}={st[KEY]} (after seeing false: {seen_false})")
    print("all flags:", {k: v for k, v in st.items()})
    break
else:
    print("TIMEOUT waiting for the flag to read true; worker:", flags()[0])
