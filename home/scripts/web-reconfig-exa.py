"""One-shot web reconfig: drop dead apinex pins (and the exa/backend pin) from all 5 configs,
copy EXA_API_KEY from the default home's .env to the three named homes that lack it.
Never prints secret values."""
import shutil, time
from pathlib import Path

import yaml
from dotenv import dotenv_values

H = Path(r"C:/Users/Ha Trung/AppData/Local/hermes")
HOMES = {"default": H, "neo_agent": H / "profiles/neo_agent", "nexus_agent": H / "profiles/nexus_agent",
         "vex_agent": H / "profiles/vex_agent", "zen_agent": H / "profiles/zen_agent"}

stamp = time.strftime("%Y%m%d-%H%M%S")
for name, home in HOMES.items():
    cfg_path = home / "config.yaml"
    raw = cfg_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw) or {}
    if not isinstance(cfg.get("web"), dict):
        print(f"{name}: no web block, skipped"); continue
    bak = cfg_path.with_suffix(f".yaml.bak-web-{stamp}")
    bak.write_text(raw, encoding="utf-8")
    old = dict(cfg["web"])
    cfg.pop("web", None)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"{name}: removed web block {sorted(old)} (backup {bak.name})")

key = (dotenv_values(str(H / ".env")) or {}).get("EXA_API_KEY", "").strip()
assert key, "default home has no EXA_API_KEY"
for name in ("nexus_agent", "vex_agent", "zen_agent"):
    env_path = HOMES[name] / ".env"
    existing = dotenv_values(str(env_path)) or {}
    if (existing.get("EXA_API_KEY") or "").strip():
        print(f"{name}: EXA_API_KEY already set"); continue
    bak = env_path.with_suffix(f".env.bak-web-{stamp}")
    shutil.copy(env_path, bak)
    text = env_path.read_text(encoding="utf-8").rstrip("\n")
    sep = "\n" if not text else "\n\n"
    env_path.write_text(text + sep + "EXA_API_KEY=" + key + "\n", encoding="utf-8")
    print(f"{name}: EXA_API_KEY added (backup {bak.name})")
