import mariadb, json, random, time, statistics, sys
import numpy as np

def connect():
    con = mariadb.connect(host="127.0.0.1", port=3306, user="hindsight", password="hindsight", database="smoke", autocommit=True)
    con.cursor().execute("SET time_zone='+00:00'")
    return con

def vtxt(v):
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"

def parse(t):
    return np.array([float(x) for x in t[1:-1].split(",")], dtype=np.float64)

def now6(cur):
    time.sleep(0.03); cur.execute("SELECT NOW(6)"); t = str(cur.fetchone()[0]); time.sleep(0.03); return t

def cos_d(M, q):
    Mn = M / np.linalg.norm(M, axis=1, keepdims=True); qn = q / np.linalg.norm(q)
    return 1.0 - Mn @ qn

def truth_topk(snap, q, k):
    ids = np.array(list(snap.keys())); M = np.array([snap[i] for i in ids])
    d = cos_d(M, q); o = np.argsort(d, kind="stable")[:k]
    return [int(ids[i]) for i in o], {int(i): float(x) for i, x in zip(ids, d)}

def run_grid(cur, table, snaps, queries, efs=(20, 80), limits=(30, 100, 200), check_np=True, label=""):
    """snaps: list of (name, clause, snapshot dict or None). Returns rows of results."""
    out = []
    for sname, clause, snap in snaps:
        for qi, (qname, q) in enumerate(queries):
            qt = vtxt(q)
            base = f"SELECT id, VEC_DISTANCE_COSINE(e, VEC_FromText(?)) d FROM {table} {clause} {{hint}} ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText(?)) LIMIT {{k}}"
            cur.execute(base.format(hint="IGNORE INDEX (e)", k=max(limits)), (qt, qt))
            exact = [r[0] for r in cur.fetchall()]
            npagree = None; valid = None; dist_np = None
            if check_np and snap is not None:
                tk, dist_np = truth_topk(snap, q, max(limits)); valid = set(snap.keys())
                npagree = {k: len(set(tk[:k]) & set(exact[:k])) / max(1, min(k, len(tk))) for k in limits}
            for ef in efs:
                cur.execute(f"SET mhnsw_ef_search={ef}")
                for k in limits:
                    cur.execute(base.format(hint="", k=k), (qt, qt))
                    rows = cur.fetchall(); ann = [r[0] for r in rows]
                    ex_k = exact[:k]
                    rec = len(set(ann) & set(ex_k)) / max(1, len(ex_k))
                    dup = len(ann) - len(set(ann))
                    invalid = len([i for i in ann if valid is not None and i not in valid])
                    stale = 0
                    if dist_np is not None:
                        stale = sum(1 for i, d in rows if i in dist_np and abs(dist_np[i] - d) > 1e-3)
                    rec_np = None
                    if snap is not None:
                        tk = truth_topk(snap, q, k)[0]
                        rec_np = len(set(ann) & set(tk)) / max(1, len(tk))
                    out.append(dict(t=label, snap=sname, q=qname, ef=ef, k=k, rows=len(ann), exact_rows=len(ex_k),
                                    recall=rec, recall_np=rec_np, dup=dup, invalid=invalid, stale=stale,
                                    np_vs_exact=(npagree or {}).get(k)))
    return out

def summarize(out):
    cells = {}
    for r in out:
        cells.setdefault((r["t"], r["snap"], r["ef"], r["k"]), []).append(r)
    lines = []
    for key, rs in cells.items():
        rows = [r["rows"] for r in rs]; ex = [r["exact_rows"] for r in rs]; rec = [r["recall"] for r in rs]
        rnp = [r["recall_np"] for r in rs if r["recall_np"] is not None]
        npx = [r["np_vs_exact"] for r in rs if r["np_vs_exact"] is not None]
        short = sum(1 for r in rs if r["rows"] < r["exact_rows"])
        worst = min(rs, key=lambda r: (r["rows"] - r["exact_rows"], r["recall"]))
        lines.append(f"{key[0]:10} {key[1]:10} ef={key[2]:<3} k={key[3]:<4} n={len(rs)} rows min/med {min(rows)}/{statistics.median(rows)} (exact min {min(ex)}) "
                     f"short={short} recall min/mean {min(rec):.3f}/{statistics.mean(rec):.3f}"
                     + (f" recall_np min {min(rnp):.3f}" if rnp else "") + (f" np_vs_exact min {min(npx):.3f}" if npx else "")
                     + f" dup={sum(r['dup'] for r in rs)} invalid={sum(r['invalid'] for r in rs)} stale={sum(r['stale'] for r in rs)} worst_q={worst['q']}")
    return lines

def explain(cur, sql, args=()):
    cur.execute("EXPLAIN " + sql, args); cols = [d[0] for d in cur.description]
    return [(r["type"], r["key"], r["rows"], r["Extra"]) for r in (dict(zip(cols, x)) for x in cur.fetchall())]
