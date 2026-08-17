"""Clean scaling benchmark: load_cache linearity + raw FTS5 at 100k rows.

Avoids the missing-files artifact by measuring load_cache directly (no
search() calls, so no _check_and_refresh side effects).
"""
import sys, time, tempfile, shutil
from pathlib import Path
sys.path.insert(0, r"C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\plugins\memory\obsidian_vault")
from vault import VaultIndex, VaultNote

BODY = ("realistic note body with project decisions and technical details " * 30)
TAGS = ["project", "hermes", "architecture"]

def build_db(tmp: Path, n: int) -> None:
    idx = VaultIndex()
    idx._init_db(tmp)
    conn = idx._db
    for i in range(n):
        note = VaultNote(path=Path(f"notes/n{i}.md"), slug=f"note-{i}", title=f"Note {i}",
                         frontmatter={"tags": TAGS}, body=BODY + f" alpha{i%997}",
                         tags=TAGS, links=[f"note-{(i+1)%n}"], backlinks=[],
                         last_modified=time.time(), size_bytes=len(BODY))
        idx._insert_note_to_db(note, tmp / "notes" / f"n{i}.md", tmp)
    conn.commit()
    conn.close()

# --- Part 1: load_cache linearity ---
for n in (3000, 10000, 30000):
    tmp = Path(tempfile.mkdtemp(prefix="vscale_"))
    try:
        build_db(tmp, n)
        idx = VaultIndex()
        t0 = time.perf_counter()
        ok = idx.load_cache(tmp)
        dt = time.perf_counter() - t0
        print(f"load_cache n={n:>6}: {dt:7.2f}s total, {dt/n*1e6:7.1f} us/note, loaded={len(idx._notes)}, ok={ok}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# --- Part 2: raw FTS5 MATCH at 100k rows (SQLite only, no plugin layer) ---
import sqlite3
tmp = Path(tempfile.mkdtemp(prefix="vfts_"))
try:
    db = tmp / "fts.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE notes(slug TEXT PRIMARY KEY, body TEXT)")
    conn.execute("CREATE VIRTUAL TABLE vault_fts USING fts5(slug, body, content='')")
    t0 = time.perf_counter()
    for i in range(100000):
        body = BODY + f" alpha{i%997} beta{i%503}"
        conn.execute("INSERT INTO notes VALUES (?,?)", (f"note-{i}", body))
        conn.execute("INSERT INTO vault_fts(rowid, slug, body) VALUES (?,?,?)", (i, f"note-{i}", body))
    conn.commit()
    print(f"\nFTS5 insert 100k rows: {time.perf_counter()-t0:.1f}s, db size: {db.stat().st_size/1e6:.0f} MB")

    queries = ["hermes architecture", "alpha42", "project decision beta7", "technical details"]
    t0 = time.perf_counter()
    reps = 100
    for _ in range(reps):
        for q in queries:
            conn.execute("SELECT slug FROM vault_fts WHERE vault_fts MATCH ? LIMIT 20", (q,)).fetchall()
    print(f"raw FTS5 MATCH @100k rows: {(time.perf_counter()-t0)/(reps*len(queries))*1e3:.2f} ms/query")
    conn.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)
