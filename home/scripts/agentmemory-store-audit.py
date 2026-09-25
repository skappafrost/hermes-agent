"""READ-ONLY audit of the agentmemory store. GETs only; never writes, never prints raw secrets."""
import json, re, sys, urllib.request, urllib.error
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:3111"
SECRET_RE = re.compile(r"(nvapi-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9\-]{10,}|ghp_[A-Za-z0-9]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}|[A-Za-z0-9_\-]{32,})")


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "replace")[:160]}
    except Exception as e:
        return None, {"_err": f"{type(e).__name__}: {e}"}


def scrub(s, n=110):
    s = str(s or "")
    s = SECRET_RE.sub(lambda m: m.group(1)[:6] + "***REDACTED***", s)
    return re.sub(r"\s+", " ", s)[:n]


def rows(payload, *keys):
    for k in keys:
        if isinstance(payload, dict):
            payload = payload.get(k, {})
    return payload if isinstance(payload, list) else []


print("=== HEALTH ===")
st, h = get("/agentmemory/health")
print(st, json.dumps(h, ensure_ascii=False)[:600])

print("\n=== FLAGS ===")
st, f = get("/agentmemory/config/flags")
print(st, json.dumps(f, ensure_ascii=False)[:600])

print("\n=== MEMORIES (all, incl orphans) ===")
st, m = get("/agentmemory/memories", {"agentId": "*", "includeOrphans": "true", "limit": "1000"})
mems = rows(m, "memories") or rows(m, "data") or rows(m, "results") or (m if isinstance(m, list) else [])
if not mems and isinstance(m, dict):
    for k, v in m.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            mems = v
            print("  (list under key %r)" % k)
            break
print("status", st, "count", len(mems))
by_agent, by_proj, by_type = Counter(), Counter(), Counter()
lens, dupes = [], Counter()
secret_hits = []
for r in mems:
    by_agent[r.get("agentId") or r.get("agent_id") or "(none)"] += 1
    by_proj[r.get("project") or "(none)"] += 1
    by_type[r.get("type") or r.get("kind") or "(none)"] += 1
    body = " ".join(str(r.get(k) or "") for k in ("content", "title", "narrative", "summary"))
    lens.append(len(body))
    dupes[re.sub(r"\W+", " ", body[:90]).lower()] += 1
    if SECRET_RE.search(body):
        secret_hits.append((r.get("obsId") or r.get("id"), r.get("agentId"), r.get("project")))
print("by agentId:", dict(by_agent))
print("by project:", dict(by_proj))
print("by type:", dict(by_type))
if lens:
    lens.sort()
    print(f"content length: min={lens[0]} med={lens[len(lens)//2]} max={lens[-1]}")
d = {k: v for k, v in dupes.items() if v > 1 and k}
print("near-duplicate groups:", len(d), "| worst:", max(d.values()) if d else 0)
print("entries containing secret-shaped tokens:", len(secret_hits))
for hid in secret_hits[:10]:
    print("   ", hid)

print("\n=== sample 12 newest memories (scrubbed) ===")
for r in mems[:12]:
    body = " ".join(str(r.get(k) or "") for k in ("title", "content", "narrative"))
    print(f"- [{r.get('agentId')}|{r.get('project')}|{r.get('type')}] {scrub(body)}")

print("\n=== SESSIONS ===")
st, s = get("/agentmemory/sessions", {"limit": "200"})
sess = rows(s, "sessions") or (s if isinstance(s, list) else [])
if not sess and isinstance(s, dict):
    for k, v in s.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            sess = v
            print("  (list under key %r)" % k)
            break
print("status", st, "count", len(sess))
c = Counter()
for x in sess:
    c[(x.get("agentId") or x.get("agent_id") or "(none)", x.get("project") or "(none)")] += 1
print("by (agentId,project):", dict(c))
turns = [x.get("observationCount", x.get("turnCount", x.get("messages"))) for x in sess]
print("counts sample:", turns[:20])
for x in sess[:12]:
    print(f"- {x.get('sessionId') or x.get('id')} agent={x.get('agentId')} proj={x.get('project')} "
          f"obs={x.get('observationCount', x.get('turnCount'))} started={str(x.get('startedAt') or x.get('createdAt'))[:19]} "
          f"cwd={scrub(x.get('cwd'), 60)}")

print("\n=== OBSERVATIONS (recent) ===")
st, o = get("/agentmemory/observations", {"limit": "300"})
obs = rows(o, "observations") or (o if isinstance(o, list) else [])
print("status", st, "count", len(obs))
hc = Counter((x.get("agentId") or "(none)", x.get("hookType") or x.get("source") or "(none)") for x in obs)
print("by (agentId,hookType):", dict(hc))

print("\n=== DERIVED: insights / lessons / crystals / slots / graph ===")
for p in ("/agentmemory/insights", "/agentmemory/lessons", "/agentmemory/crystals", "/agentmemory/slots", "/agentmemory/graph/stats"):
    st, v = get(p)
    txt = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    print(f"{p} -> {st} {scrub(txt, 300)}")
