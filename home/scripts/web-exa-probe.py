"""Live probe: does exa serve both search and extract? Runs against a pinless temp home."""
import os, sys, json, time, asyncio, shutil
from pathlib import Path

import yaml

H = Path(r"C:/Users/Ha Trung/AppData/Local/hermes")
TEST = H / ".webtest-exa"
TEST.mkdir(exist_ok=True)
shutil.copy(H / ".env", TEST / ".env")
cfg = yaml.safe_load((H / "config.yaml").read_text(encoding="utf-8")) or {}
cfg.pop("web", None)  # Hermes default: autodetect ladder + keyless rescue
(TEST / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

os.environ["HERMES_HOME"] = str(TEST)
sys.path.insert(0, str(H / "hermes-agent"))

from dotenv import dotenv_values  # noqa: E402
for k, v in (dotenv_values(str(TEST / ".env")) or {}).items():
    if v and k not in os.environ:
        os.environ[k] = v

import tools.web_tools as W  # noqa: E402

print("search_backend =", W._get_search_backend(), "| extract_backend =", W._get_extract_backend())
print("exa available  =", W._is_backend_available("exa"))

stamp = str(int(time.time()))
t = time.time()
r = json.loads(W.web_search_tool(f"python asyncio structured concurrency probe {stamp}", limit=3))
pages = r.get("web_pages") or r.get("results") or []
print(f"SEARCH {'OK' if pages else 'EMPTY'} {time.time()-t:.1f}s n={len(pages)} err={r.get('error')}")

URLS = ["https://docs.python.org/3/tutorial/index.html",
        "https://en.wikipedia.org/wiki/Python_(programming_language)"]
t = time.time()
raw = asyncio.run(W.web_extract_tool(URLS))
d = json.loads(raw)
res = d.get("results", [])
ok = [p for p in res if p.get("content") and not p.get("error")]
print(f"EXTRACT {time.time()-t:.1f}s ok={len(ok)}/{len(res)} chars={sum(len(str(p.get('content',''))) for p in ok)}")
for p in res:
    print("   -", str(p.get("url"))[:55], "|", "ERR " + str(p.get("error"))[:80] if p.get("error") else str(p.get("title"))[:50])
