"""Live test of the rewritten capture path, under a throwaway agentId so real profiles stay clean.

Checks: turn -> prompt_submit observation (session-linked, agentId inherited), Hermes control-frame
rows NOT captured, session closed at on_session_end, recall formatted from the nested observation.
"""
import importlib.util, json, sys, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
H = Path(r"C:/Users/Ha Trung/AppData/Local/hermes")
B = "http://127.0.0.1:3111"
os_home = str(H)
import os
os.environ["HERMES_HOME"] = os_home
sys.path.insert(0, str(H / "hermes-agent"))

spec = importlib.util.spec_from_file_location("am_provider", str(H / "plugins/agentmemory/__init__.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SID = f"selftest-{int(time.time())}"
AGENT = "provider-selftest"
p = mod.AgentMemoryProvider()
p.initialize(SID, agent_identity=AGENT, agent_context="primary", cwd=str(H / "Documents"))

p.sync_turn("Quyết định của tôi: memory provider sẽ ghi qua /observe thay vì /remember để engine nén được.",
            "Đã đổi sang observe, mỗi turn một observation gắn sessionId.")
p.sync_turn("[ASYNC DELEGATION BATCH COMPLETE — deleg_selftest]\nA background fan-out unit you dispatched earlier has finished.",
            "Đây là thông báo nội bộ, không phải lời người dùng.")
p.sync_turn("Và tôi muốn viewer hiển thị session có liên kết tới memory.", "Đã thêm liên kết sessionId.")
time.sleep(6)
p.on_session_end([])
time.sleep(4)


def get(path):
    try:
        with urllib.request.urlopen(B + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_err": str(e)[:150]}


def post(path, body):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


sess = [x for x in (get("/agentmemory/sessions?limit=50").get("sessions") or []) if x.get("id") == SID]
s = sess[0] if sess else {}
print(f"1. session row        : found={bool(s)} agentId={s.get('agentId')} project={s.get('project')}")
print(f"2. observationCount   : {s.get('observationCount')}  (expect 2 — the delegation row must be skipped)")
print(f"3. status/endedAt     : {s.get('status')} / {s.get('endedAt')}  (expect completed via on_session_end)")

obs = (get(f"/agentmemory/observations?sessionId={SID}").get("observations") or [])
print(f"4. observations stored: {len(obs)}")
for o in obs:
    c = o.get("compressed") or o
    print(f"    - hook={c.get('hookType')} agentId={c.get('agentId')} type={c.get('type')} "
          f"title={str(c.get('title'))[:58]!r}")
    print(f"      facts={str(c.get('facts'))[:150]}")

print("5. leaked delegation row:", any("deleg_selftest" in json.dumps(o, ensure_ascii=False) for o in obs))

r = post("/agentmemory/search", {"query": "memory provider ghi qua observe remember", "format": "full",
                                 "limit": 5, "agentId": AGENT})
print(f"6. recall own agent   : hits={len(r.get('results') or [])}")
print("   provider-formatted:\n" + "\n".join("     " + l for l in (p._format(r) or "(empty)").splitlines()))
other = post("/agentmemory/search", {"query": "memory provider ghi qua observe remember", "format": "full",
                                     "limit": 5, "agentId": "neo_agent"})
leak = [h for h in (other.get("results") or []) if (h.get("observation") or {}).get("agentId") == AGENT]
print(f"7. cross-profile leak : {len(leak)} rows (expect 0)")
