"""Archive neo_agent's stored memories, then delete only the Hermes control-frame junk rows.

Deletion is by exact title match against the captured list printed first; everything is written to
a JSON backup before any DELETE, so the purge is reversible via POST /agentmemory/import.
"""
import json, re, sys, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
B = "http://127.0.0.1:3111"
AGENT = "neo_agent"
# Hermes delivers these as role:user rows; the old capture path stored them verbatim.
JUNK = re.compile(r"^\s*\[?(ASYNC DELEGATION|CONTEXT COMPACTION|PRIOR CONTEXT|System:|Runtime note:)", re.I)


def req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data,
                               headers={"Content-Type": "application/json"},
                               method="POST" if data else "GET")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


mems = req(f"/agentmemory/memories?agentId={AGENT}&includeOrphans=true&limit=200").get("memories") or []
sess = [x for x in req("/agentmemory/sessions?limit=100").get("sessions") or [] if x.get("agentId") == AGENT]
obs = []
for s in sess:
    obs += req(f"/agentmemory/observations?sessionId={s['id']}").get("observations") or []

bak = Path.home() / "AppData/Roaming/agentmemory" / f"pre-junk-purge-{time.strftime('%Y%m%d-%H%M%S')}.json"
bak.parent.mkdir(exist_ok=True)
bak.write_text(json.dumps({"exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "agentId": AGENT, "memories": mems, "sessions": sess, "observations": obs},
                          ensure_ascii=False, indent=1), encoding="utf-8")
print(f"backup -> {bak} ({bak.stat().st_size:,} bytes) memories={len(mems)} sessions={len(sess)} observations={len(obs)}")

doomed = [m for m in mems if JUNK.match(str(m.get("title") or "")) or JUNK.match(str(m.get("content") or ""))]
print(f"\nwill delete {len(doomed)} of {len(mems)}:")
for m in doomed:
    print(f"  {m['id']}  {str(m.get('title'))[:72]!r}")
for m in mems:
    if m not in doomed:
        print(f"  keep {m['id']} v{m.get('version')} latest={m.get('isLatest')} {str(m.get('title'))[:60]!r}")

if "--apply" not in sys.argv:
    print("\nDRY RUN (pass --apply to delete)")
    sys.exit(0)

for m in doomed:
    print("delete", m["id"], "->", req("/agentmemory/forget", {"memoryId": m["id"]}))

after = req(f"/agentmemory/memories?agentId={AGENT}&includeOrphans=true&limit=200").get("memories") or []
print(f"\nremaining memories: {len(after)} (was {len(mems)})")
still = [m["id"] for m in after if JUNK.match(str(m.get("title") or ""))]
print("junk left:", still or "none")
