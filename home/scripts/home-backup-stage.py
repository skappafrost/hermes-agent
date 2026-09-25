"""Stage a redacted home-state backup for the fork push.

Publishes code + config *structure* only: every credential-looking value is masked, and the paths
that hold upstream content or old key-bearing snapshots are excluded. Writes nothing outside the
staging dir and does not touch the live hermes-agent worktree.
"""
import os
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
H = Path(r"C:/Users/Ha Trung/AppData/Local/hermes")
STAGE = Path(r"C:/Users/Ha Trung/AppData/Local/Temp/home-backup-stage")
SKIP_DIR = re.compile(r"(^|[\\/])(__pycache__|node_modules|\.venv|\.git|\.pytest_cache|state-snapshots|\.archive|logs|sessions|memory)([\\/]|$)")
SKIP_EXT = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".exe", ".dll", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".map"}

# Value-level masking. Field names are normalised so `X-Goog-Api-Key` matches like `api_key` does,
# and any scalar that looks like a vendor credential is masked regardless of field name — GitHub
# push-protection keys off the VALUE shape, not the key name, so a name-based filter is not enough.
NAME_HINT = re.compile(r"(api[-_]?key|token|secret|passw|credential|access[-_]?key|auth|[-_]key)", re.I)
KV_LINE = re.compile(r"^(\s*(?:-\s+)?[\w.$()-]*)([:=])(\s*)(.+?)\s*$")
VENDOR = re.compile(r"^(AIza|AQ\.|ya29\.|nvapi-|sk-|gsk_|ghp_|github_pat_|gho_|ghs_|cfut_|apk_|fw_|AKIA|xox[baprs]-|EAA|eyJ|uf_|sk_live|pk_live)", re.I)
NOT_A_SECRET = re.compile(r"^(https?://|file:|\.{1,2}[\\/]|[A-Za-z]:[\\/]|REDACTED|\$\{|<|\{\}|\[\]|true|false|null|none)$", re.I)
PURE_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")


def looks_secret(val: str) -> bool:
    v = val.strip().strip("'\"")
    if len(v) < 16 or "/" in v or "\\" in v or PURE_HEX.match(v) or NOT_A_SECRET.match(v):
        return False
    if VENDOR.match(v):
        return True
    return sum(c.isdigit() for c in v) >= 3 and sum(c.isalpha() for c in v) >= 4 and len(v) >= 20


def mask_text(txt):
    n = 0
    out = []
    pending_indent = None      # credential key whose value is a folded/block scalar below it
    for line in txt.splitlines(True):
        raw = line.rstrip("\n")
        m = KV_LINE.match(raw)
        if pending_indent is not None:
            indent = len(raw) - len(raw.lstrip())
            if not raw.strip():
                out.append(raw)
                continue
            if indent <= pending_indent:
                pending_indent = None          # block ended; fall through and process normally
            elif KV_LINE.match(raw) and NAME_HINT.search(raw.split(":", 1)[0]):
                # A nested mapping (`api_keys:` then `foo: bar`), not a folded scalar — keep it.
                pending_indent = None
            else:
                out.append(" " * indent + "REDACTED")
                n += 1
                continue
        if m:
            name, sep, gap, val = m.group(1), m.group(2), m.group(3), m.group(4)
            stripped = val.strip().strip("'\"")
            if stripped in {"|", "|-", "|+", ">", ">-", ">+"}:
                stripped = ""
            hit = looks_secret(stripped) or (NAME_HINT.search(name) and len(stripped) >= 8
                                             and not stripped.startswith(("${", "<", "REDACTED")))
            if hit and stripped:
                out.append(f"{name}{sep}{gap}REDACTED")
                n += 1
                continue
            if NAME_HINT.search(name) and not stripped:
                pending_indent = len(name) - len(name.lstrip()) if name.strip() else 0
        out.append(raw)
    return "\n".join(out) + ("\n" if txt.endswith("\n") and out else ""), n


def copy_file(src, dst, mask):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mask:
        try:
            txt = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(src, dst)
            return "binary"
        masked, cnt = mask_text(txt)
        dst.write_text(masked, encoding="utf-8")
        return f"masked:{cnt}"
    shutil.copy2(src, dst)
    return "copied"


report = {"files": 0, "bytes": 0, "masked_files": 0, "mask_count": 0, "skipped_binary": 0}
if STAGE.exists():
    shutil.rmtree(STAGE)
STAGE.mkdir(parents=True)


def add_tree(src_root, dst_root, mask=False):
    for dp, dns, fns in os.walk(src_root):
        dns[:] = [d for d in dns if not SKIP_DIR.search(os.path.join(dp, d))]
        for f in fns:
            p = Path(dp) / f
            if p.suffix.lower() in SKIP_EXT:
                continue
            r = copy_file(p, dst_root / p.relative_to(src_root), mask)
            report["files"] += 1
            report["bytes"] += p.stat().st_size
            if r.startswith("masked:"):
                report["mask_count"] += int(r.split(":")[1])
                report["masked_files"] += 1
            elif r == "binary":
                report["skipped_binary"] += 1


add_tree(H / "plugins", STAGE / "home/plugins")
add_tree(H / "scripts", STAGE / "home/scripts")

HOMES = {"default": H, "neo_agent": H / "profiles/neo_agent", "nexus_agent": H / "profiles/nexus_agent",
         "vex_agent": H / "profiles/vex_agent", "zen_agent": H / "profiles/zen_agent"}
for name, home in HOMES.items():
    for f in sorted(home.glob("*.md")) + [home / "config.yaml"]:
        if f.is_file():
            r = copy_file(f, STAGE / f"home/profiles/{name}/{f.name}", mask=f.name == "config.yaml")
            report["files"] += 1
            report["bytes"] += f.stat().st_size
            if r.startswith("masked:"):
                report["mask_count"] += int(r.split(":")[1])
                report["masked_files"] += 1
    env = home / ".env"
    if env.is_file():
        keys = [ln.split("=", 1)[0].strip() for ln in env.read_text(encoding="utf-8", errors="ignore").splitlines()
                if ln.strip() and not ln.strip().startswith("#") and "=" in ln]
        (STAGE / f"home/profiles/{name}").mkdir(parents=True, exist_ok=True)
        (STAGE / f"home/profiles/{name}/env.keys.txt").write_text(
            "# key NAMES present in .env (values intentionally not backed up)\n" + "\n".join(sorted(keys)) + "\n",
            encoding="utf-8")
        report["files"] += 1
        print(f"  {name}: config.yaml masked, .env -> names only ({len(keys)} keys)")

am = Path.home() / ".agentmemory" / ".env"
if am.is_file():
    txt = am.read_text(encoding="utf-8")
    masked, cnt = mask_text(txt)
    (STAGE / "agentmemory").mkdir(parents=True, exist_ok=True)
    (STAGE / "agentmemory/env.masked").write_text(masked, encoding="utf-8")
    report["files"] += 1
    report["mask_count"] += cnt
    report["masked_files"] += 1
    print(f"  ~/.agentmemory/.env masked ({cnt} values)")

print(f"\nstaged: {report['files']} files, {report['bytes']/1048576:.2f} MiB, "
      f"{report['masked_files']} masked files / {report['mask_count']} values, {report['skipped_binary']} binary-as-is")
(STAGE / "STAGED_TREE.txt").write_text("\n".join(sorted(str(p.relative_to(STAGE)) for p in STAGE.rglob("*") if p.is_file())), encoding="utf-8")

# --- publish gate: replicate what GitHub push-protection will do, before we push ---------------
SHAPES = re.compile(r"\b(AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-\.]{20,}|ya29\.[0-9A-Za-z_\-]{20,}|nvapi-[A-Za-z0-9_\-]{16,}|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,}|xox[baprs]-[A-Za-z0-9\-]{18,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}|EAA[A-Za-z0-9]{20,})\b")
flagged = []
for p in STAGE.rglob("*"):
    if not p.is_file() or p.suffix.lower() in {".png", ".jpg", ".ico", ".zip", ".exe", ".dll", ".woff", ".woff2", ".map"}:
        continue
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for m in SHAPES.finditer(txt):
        flagged.append((str(p.relative_to(STAGE)), txt[:m.start()].count("\n") + 1, m.group(0)[:4] + "…"))
    if p.suffix.lower() in {".yaml", ".yml"} or p.name in ("env.masked", "env.keys.txt"):
        for i, line in enumerate(txt.splitlines(), 1):
            kv = re.match(r"^\s*[\w.$()\"'-]*([:=])\s*(.*)$", line)
            if kv and looks_secret(kv.group(2)):
                flagged.append((str(p.relative_to(STAGE)), i, f"entropy value in {line.split(kv.group(1))[0].strip()[:24]!r}"))
if flagged:
    print(f"\nGATE FAILED — {len(flagged)} secret-shaped value(s) still staged (do NOT push):")
    for f in flagged[:20]:
        print("   ", f)
    sys.exit(1)
print("GATE OK — no secret-shaped value left in the staged tree")
