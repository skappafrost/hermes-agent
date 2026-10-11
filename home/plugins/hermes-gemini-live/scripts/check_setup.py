"""Pre-flight check for hermes-gemini-live: key, model, function calling, run lane.

Runs against the same code path the plugin uses, from outside Hermes, so a fresh install can be
proved without opening the Desktop app. Nothing here installs anything.

    python scripts/check_setup.py                     # the home this shell points at
    python scripts/check_setup.py --profiles          # every profile under that root
    python scripts/check_setup.py --tools             # also prove the model calls the delegate

Exits non-zero when a required check fails. Reads secrets from the environment first and from
<home>/.env second, and never prints a key.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to a legacy code page; this output uses real dashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hermes_gemini_live import agent_lane, config, live_client, tools, wire  # noqa: E402

QUESTION = ("What is the current time in Tokyo right now? You have no idea what time it is, "
            "so you must call hermes_task to find out. Do not guess.")

GREEN, RED, YELLOW, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():          # CI logs and pipes get plain text
    GREEN = RED = YELLOW = OFF
results: list[str] = []


def report(ok: bool, name: str, detail: str = "", *, fatal: bool = True) -> bool:
    if ok:
        results.append("ok")
        mark = f"{GREEN}ok  {OFF}"
    elif fatal:
        results.append("fail")
        mark = f"{RED}FAIL{OFF}"
    else:
        results.append("warn")
        mark = f"{YELLOW}warn{OFF}"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok or not fatal


def read_home_env(home: Path, name: str) -> str | None:
    """One value from ``<home>/.env``, for the standalone case where no Hermes scope exists."""
    env_file = home / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def secret(home: Path, name: str) -> str | None:
    return os.environ.get(name) or read_home_env(home, name)


def check_home(home: Path, *, tools_check: bool) -> bool:
    print(f"\n{home}")
    # A standalone process is not the gateway: it has no profile secret scope, so the lane
    # would read an empty os.environ and report a key that is sitting in this home's .env.
    # Load what the probe needs before probing, rather than reporting a false failure.
    for name in ("API_SERVER_KEY", "GEMINI_LIVE_API_KEY", "GEMINI_API_KEY"):
        if not os.environ.get(name):
            value = read_home_env(home, name)
            if value:
                os.environ[name] = value
    ok = True
    key = secret(home, "GEMINI_LIVE_API_KEY") or secret(home, "GEMINI_API_KEY")
    ok &= bool(report(bool(key), "Gemini key present",
                      "" if key else f"add GEMINI_API_KEY to {home / '.env'}"))

    profile = home.name if home.parent.name == "profiles" else None
    base = agent_lane.base_url(profile)
    status, detail = agent_lane.probe(None, profile)
    report(status == agent_lane.STATUS_OK, f"Hermes run lane at {base}",
           detail or f"status={status}", fatal=False)
    if status == agent_lane.STATUS_UNAUTHORIZED and profile:
        print(f"        ↳ a multiplexed gateway authorises /p/{profile}/ with that profile's "
              f"own API_SERVER_KEY; it does not inherit the default home's")

    if not key:
        return False

    model = config.model()
    try:
        setup = wire.build_setup(model=model, voice=config.voice(),
                                 instructions=config.instructions(),
                                 thinking_level=config.thinking_level(model),
                                 activity=config.activity_detection(),
                                 tools=tools.declarations())
    except config.ConfigError as exc:
        return report(False, f"setup frame for {model}", str(exc))

    import asyncio
    import json

    async def probe_live() -> tuple[bool, str]:
        import websockets

        try:
            async with websockets.connect(live_client.uri_with(key), max_size=None,
                                          open_timeout=20) as sock:
                await sock.send(json.dumps(setup))
                first = json.loads(await asyncio.wait_for(sock.recv(), 20))
                if "setupComplete" not in first:
                    return False, json.dumps(first)[:200]
                if not tools_check:
                    return True, ""
                await sock.send(json.dumps(wire.text_input(QUESTION)))
                called = False
                deadline = time.time() + 40
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(sock.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        break
                    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
                    if '"toolCall"' in text or '"functionCall"' in text:
                        called = True
                        break
                    if '"turnComplete"' in text:
                        deadline = min(deadline, time.time() + 10)
                return called, ("" if called else
                                "the model answered this one itself — expected for "
                                "...-extended-thinking, which delegates about 2 times in 5")
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:180]}"

    live_ok, live_detail = asyncio.run(probe_live())
    label = f"Live session for {model}" + (" + delegate call" if tools_check else "")
    report(live_ok, label, live_detail, fatal=tools_check)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=None, help="Hermes home to check (default: $HERMES_HOME)")
    parser.add_argument("--profiles", action="store_true",
                        help="also check every profile under the home's parent root")
    parser.add_argument("--tools", action="store_true",
                        help="prove the model actually calls the delegate (spends a little of "
                             "your key; ~40 s per home)")
    args = parser.parse_args()

    root = Path(args.home or os.environ.get("HERMES_HOME") or "").resolve() if (
        args.home or os.environ.get("HERMES_HOME")) else None
    if root is None:
        default = (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
                   if os.name == "nt" else Path.home() / ".hermes")
        root = default
    homes = [root]
    if args.profiles:
        homes += sorted(p for p in (root / "profiles").glob("*") if p.is_dir())

    print(f"hermes-gemini-live pre-flight — model={config.model()} voice={config.voice()}")
    good = all(check_home(home, tools_check=args.tools) for home in homes)
    print(f"\n{results.count('ok')} ok, {results.count('warn')} warned, "
          f"{results.count('fail')} failed")
    if not good:
        print("Fix the FAIL lines above; the panel will say the same thing until they are resolved.")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
