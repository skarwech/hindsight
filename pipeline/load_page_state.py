"""Step 1 (F2): every version's metadata from dataset M into wikihistory.page_state, with real edit times.

    .venv/bin/python pipeline/load_page_state.py            # resumable: loaded pages are skipped
    .venv/bin/python pipeline/load_page_state.py --fresh    # drop and reload (for timing)

One worker per Parquet file (a page lives in exactly one file). Each batch of whole pages is one
transaction, so a killed run never leaves half a page. Flags are computed here because an UPDATE
on a versioned row would write a history row stamped with the real clock.
"""
import argparse, glob, itertools, json, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from common import connect, split_sql  # noqa: E402  (shared DB helpers live in eval/common.py)
from revisions import bumped_starts, flags, fmt  # noqa: E402

BATCH_ROWS = 5000          # ~1 MB of metadata per transaction, far under max_allowed_packet
COLS = "page_id, title, rev_id, editor, anon, minor, comment, sha1, bytes, is_revert, undone, row_start"
READ = """SELECT page_id, title, rev_id, "user", COALESCE(anonymous, false), COALESCE(minor, false), comment, sha1, bytes,
                 timezone('UTC', "timestamp") AS ts
          FROM read_parquet(?) ORDER BY page_id, ts, rev_id"""


def page_rows(versions: list[tuple]) -> tuple[list[tuple], tuple, int]:
    """History rows, the current row, and how many same-second edits were bumped, for one page."""
    times: list[datetime] = [v[9] for v in versions]
    starts = bumped_starts(times)
    is_rev, undone = flags([v[7] for v in versions], starts)
    rows = [v[:9] + (is_rev[i], undone[i], fmt(starts[i])) for i, v in enumerate(versions)]
    hist = [r + (fmt(starts[i + 1]),) for i, r in enumerate(rows[:-1])]
    return hist, rows[-1], sum(s != t for s, t in zip(starts, times))


def load_file(path: str, done: frozenset[int]) -> dict:
    t0 = time.time()
    versions = duckdb.connect().execute(READ, [path]).fetchall()
    con = connect("wikihistory"); con.autocommit = False
    cur = con.cursor(); cur.execute("SET system_versioning_insert_history = 1")
    stats = dict(file=Path(path).name, pages=0, skipped=0, rows=0, bumped=0, reverts=0, undone=0)
    hist, cur_rows = [], []

    def flush() -> None:
        if hist:
            cur.executemany(f"INSERT INTO page_state ({COLS}, row_end) VALUES ({', '.join('?' * 13)})", hist)
        if cur_rows:
            cur.executemany(f"INSERT INTO page_state ({COLS}) VALUES ({', '.join('?' * 12)})", cur_rows)
        con.commit(); hist.clear(); cur_rows.clear()

    for page_id, group in itertools.groupby(versions, key=lambda v: v[0]):
        vs = list(group)
        if page_id in done:
            stats["skipped"] += 1; continue
        h, c, bumped = page_rows(vs)
        hist.extend(h); cur_rows.append(c)
        stats["pages"] += 1; stats["rows"] += len(vs); stats["bumped"] += bumped
        stats["reverts"] += sum(r[9] for r in h) + c[9]; stats["undone"] += sum(r[10] for r in h) + c[10]
        if len(hist) + len(cur_rows) >= BATCH_ROWS:
            flush()
    flush(); con.close()
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="drop page_state first")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    cur = connect("wikihistory").cursor()
    if args.fresh:
        cur.execute("DROP TABLE IF EXISTS page_state")
    ddl = next(s for s in split_sql((ROOT / "db/schema.sql").read_text(encoding="utf-8")) if s.startswith("CREATE TABLE page_state"))
    cur.execute(ddl.replace("CREATE TABLE page_state", "CREATE TABLE IF NOT EXISTS page_state", 1))
    cur.execute("SELECT DISTINCT page_id FROM page_state FOR SYSTEM_TIME ALL")
    done = frozenset(r[0] for r in cur.fetchall())
    files = sorted(glob.glob(str(ROOT / "data/m/versions-*.parquet")))
    if len(files) != 16:
        sys.exit(f"expected the 16 Parquet files of dataset M in data/m, found {len(files)}")
    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as pool:                  # a failing worker re-raises here
        results = list(pool.map(load_file, files, [done] * len(files)))
    total = {k: sum(r[k] for r in results) for k in ("pages", "skipped", "rows", "bumped", "reverts", "undone")}
    total.update(seconds=round(time.time() - t0, 1), workers=args.workers, fresh=args.fresh, files=results)
    print(json.dumps({k: v for k, v in total.items() if k != "files"}))
    if total["pages"]:                                               # a no-op resume must not overwrite the timing
        (ROOT / "logs").mkdir(exist_ok=True)
        (ROOT / "logs/page_state_load.json").write_text(json.dumps(total, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
