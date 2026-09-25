"""Detail dump of the 9 stored memories: ids, timestamps, session linkage, embedding presence, dedupe clusters."""
import json, re, sys, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:3111"
SECRET_RE = re.compile(r"(nvapi-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,})")


def get(path, params=None):
    url = BASE + path + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


d = get("/agentmemory/memories", {"agentId": "*", "includeOrphans": "true", "limit": "1000"})
mems = d.get("memories") or d.get("data") or []
if not mems:
    for k, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            mems = v
            break
print("KEYS of one record:", sorted(mems[0].keys()) if mems else "none")
print()
for m in mems:
    body = str(m.get("content") or m.get("narrative") or "")
    emb = m.get("embedding") or m.get("embeddingVector") or m.get("vector")
    print(f"id={m.get('obsId') or m.get('id')} ts={str(m.get('createdAt') or m.get('timestamp'))[:25]} "
          f"agent={m.get('agentId')} proj={m.get('project')} sid={m.get('sessionId') or m.get('session_id') or '-'} "
          f"type={m.get('type')} conf={m.get('confidence')} embed={'yes' if emb else 'no/' + str(m.get('embeddingModel'))} "
          f"len={len(body)}")
    print("   TITLE:", re.sub(r"\s+", " ", str(m.get('title') or ''))[:120])
    print("   BODY :", SECRET_RE.sub("***", re.sub(r"\s+", " ", body))[:200])

print("\n=== dedupe clusters (first 120 normalized chars) ===")
cl = defaultdict(list)
for m in mems:
    key = re.sub(r"\W+", " ", str(m.get("content") or ""))[:120].strip().lower()
    cl[key].append((m.get("obsId") or m.get("id"), str(m.get("createdAt"))[:25]))
for k, v in sorted(cl.items(), key=lambda kv: -len(kv[1])):
    if len(v) > 1:
        print(f"x{len(v)}  {k[:80]}")
        for vid, ts in v:
            print(f"      {vid}  {ts}")

print("\n=== per-agentId probe via search ===")
for a in ("default", "neo_agent", "nexus_agent", "vex_agent", "zen_agent"):
    body = json.dumps({"query": "file markdown", "format": "compact", "limit": 20, "agentId": a}).encode()
    req = urllib.request.Request(BASE + "/agentmemory/search", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        n = len(res.get("results") or [])
        print(f"  {a:12s} search hits={n}")
    except Exception as e:
        print(f"  {a:12s} ERR {e}")
