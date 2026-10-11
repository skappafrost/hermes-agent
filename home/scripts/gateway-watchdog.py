"""Hermes gateway watchdog (Windows).

Task Scheduler's RestartOnFailure demonstrably does NOT resurrect tasks on
this machine (verified 2026-09-06 with an isolated dummy task: exit code 75
propagated correctly, Last Result=75, but no restart after 4+ minutes even
with UseUnifiedSchedulingEngine). This script is the external safety net:
run it every few minutes via a scheduled task; it checks each gateway's
heartbeat and re-runs the corresponding Hermes_Gateway* task when dead.

Safety rules:
- Skips a gateway whose planned-stop marker (<home>/.gateway-planned-stop.json,
  TTL 60s, written by `hermes gateway stop`) is fresh: that's an intentional
  stop and the watchdog must not fight it.
- MultipleInstancesPolicy=IgnoreNew on the gateway tasks makes a /Run while
  the gateway is still starting a harmless no-op.
- Heartbeat threshold 240s (beat interval is 120s, so 2x + margin).
- HEARTBEAT_FRESH_S = max(240, 3 * <interval written in heartbeat JSON>) per
  home: the beat cadence differs between hosts, so trust the file over the
  constant to avoid false rescues (2026-09-08 incident: healthy gateways were
  "rescued" every ~2 min because the threshold was below the actual beat
  interval).
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"C:\Users\Ha Trung\AppData\Local\hermes")
TARGETS = [
    ("Hermes_Gateway", ROOT),
    ("Hermes_Gateway_vex_agent", ROOT / "profiles" / "vex_agent"),
    ("Hermes_Gateway_neo_agent", ROOT / "profiles" / "neo_agent"),
    ("Hermes_Gateway_nexus_agent", ROOT / "profiles" / "nexus_agent"),
    ("Hermes_Gateway_zen_agent", ROOT / "profiles" / "zen_agent"),
]
HEARTBEAT_FRESH_S = 240
PLANNED_STOP_GRACE_S = 120
LOG_PATH = ROOT / "logs" / "gateway-watchdog.log"


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")
    except OSError:
        pass


def _parse_iso(ts: str) -> float:
    dt = datetime.fromisoformat(ts.replace("+00:00", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def heartbeat_info(home: Path):
    """Return (age_seconds, beat_interval_seconds or None) for a gateway home."""
    hb = home / "state" / "gateway.heartbeat"
    try:
        data = json.loads(hb.read_text(encoding="utf-8"))
        age = time.time() - _parse_iso(data["updated_at"])
        interval = data.get("interval_s") or data.get("interval")
        interval = float(interval) if interval else None
        return age, interval
    except (OSError, ValueError, KeyError, TypeError):
        return float("inf"), None


def planned_stop_recent(home: Path) -> bool:
    marker = home / ".gateway-planned-stop.json"
    try:
        return (time.time() - marker.stat().st_mtime) < PLANNED_STOP_GRACE_S
    except OSError:
        return False


def main() -> int:
    actions = 0
    for task_name, home in TARGETS:
        if not home.is_dir():
            continue
        age, interval = heartbeat_info(home)
        # The gateway's own heartbeat file tells us its beat cadence; require
        # at least 3 missed beats before declaring it dead. Constant is only
        # a floor for files without an interval field.
        threshold = max(HEARTBEAT_FRESH_S, (interval or 0) * 3)
        if age <= threshold:
            continue
        if planned_stop_recent(home):
            log(f"{task_name}: heartbeat stale ({age:.0f}s > {threshold:.0f}s) but planned-stop marker is fresh -> skip")
            continue
        r = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True, text=True, timeout=30,
        )
        ok = r.returncode == 0
        log(f"{task_name}: heartbeat stale ({age:.0f}s > {threshold:.0f}s) -> schtasks /Run rc={r.returncode} {r.stdout.strip()!r}")
        actions += 1 if ok else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
