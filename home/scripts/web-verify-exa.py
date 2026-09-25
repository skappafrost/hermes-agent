"""Per-home web verdict: resolves search/extract backend from that home's config+.env and runs one
live search + one live extract. Usage: web-verify-exa.py <home-dir>"""
import os, sys, json, time, asyncio
from pathlib import Path

from dotenv import dotenv_values

home = Path(sys.argv[1])
os.environ["HERMES_HOME"] = str(home)
for k, v in (dotenv_values(str(home / ".env")) or {}).items():
    if v and k not in os.environ:
        os.environ[k] = v
sys.path.insert(0, str(Path(r"C:/Users/Ha Trung/AppData/Local/hermes/hermes-agent")))
import tools.web_tools as W  # noqa: E402

sk, ek = W._get_search_backend(), W._get_extract_backend()
t = time.time()
r = json.loads(W.web_search_tool(f"python 3.14 release notes probe {int(time.time())}", limit=3))
hits = (r.get("data") or {}).get("web") or []
sv = f"OK n={len(hits)}" if hits else f"FAIL {str(r.get('error'))[:80] or 'empty'}"
sd = time.time() - t
t = time.time()
d = json.loads(asyncio.run(W.web_extract_tool(["https://docs.python.org/3/tutorial/index.html"])))
res = d.get("results", [])
ev = f"OK {len(res[0].get('content',''))}c" if res and res[0].get("content") else f"FAIL {str(res[0].get('error'))[:60] if res else 'none'}"
print(f"{home.name:12s} search[{sk}] {sv} ({sd:.1f}s) | extract[{ek}] {ev} ({time.time()-t:.1f}s)")
