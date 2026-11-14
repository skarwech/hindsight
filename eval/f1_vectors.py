"""F1 vector smoke tests: T3 at scale, T4 headline query with per-leg routing and a time oracle, T5 parity.
Called from f1_check.py. Hardened after the adversarial review of 2026-11-13 (see docs/findings.md)."""
import json, re, time
from datetime import datetime

import mariadb
import numpy as np

from common import ROOT, Checks, connect, embed, split_sql


# ---------- T3: HNSW on a system-versioned table, at a scale where MDEV-35246 shows up ----------
def _vt(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def _ann_vs_exact(cur: mariadb.Cursor, clause: str, queries: list[np.ndarray], k: int = 30) -> dict:
    sql = f"SELECT id FROM t3s {clause} {{hint}} ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText(?)) LIMIT {k}"
    rows, recall, dups = [], [], 0
    for q in queries:
        cur.execute(sql.format(hint=""), (_vt(q),)); ann = [r[0] for r in cur.fetchall()]
        cur.execute(sql.format(hint="IGNORE INDEX (e)"), (_vt(q),)); exact = [r[0] for r in cur.fetchall()]
        rows.append(len(ann)); dups += len(ann) - len(set(ann))
        recall.append(len(set(ann) & set(exact)) / max(1, len(exact)))
    return dict(n=len(queries), short=sum(r < k for r in rows), min_rows=min(rows),
                min_recall=round(min(recall), 2), mean_recall=round(float(np.mean(recall)), 2), dups=dups)


def _churned_table(cur: mariadb.Cursor, versioned: bool, rng: np.random.Generator, n: int,
                   dim: int) -> tuple[str, list[np.ndarray], list[np.ndarray]]:
    centers = rng.normal(size=(20, dim))
    cl = rng.integers(0, 20, n); X = centers[cl] + 0.35 * rng.normal(size=(n, dim))
    cur.execute("DROP TABLE IF EXISTS t3s")
    cur.execute(f"CREATE TABLE t3s (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL, VECTOR INDEX (e) M=16 DISTANCE=cosine)"
                + (" WITH SYSTEM VERSIONING" if versioned else ""))
    for i in range(0, n, 2000):
        cur.executemany("INSERT INTO t3s VALUES (?, VEC_FromText(?))", [(j, _vt(X[j])) for j in range(i, min(n, i + 2000))])
    time.sleep(0.02); cur.execute("SELECT NOW(6)"); before = str(cur.fetchone()[0]); time.sleep(0.02)
    hot = np.flatnonzero(cl < 2)                                       # one hot region, edited over and over
    upd = list(rng.choice(hot, 10000)) + list(rng.choice(n, 8500))
    cur.executemany("UPDATE t3s SET e = VEC_FromText(?) WHERE id = ?",
                    [(_vt(centers[cl[i]] + 0.35 * rng.normal(size=dim)), int(i)) for i in upd])
    gone = rng.choice(n, 3000, replace=False)
    cur.executemany("DELETE FROM t3s WHERE id = ?", [(int(i),) for i in gone])
    old = [X[i] for i in rng.choice(list(set(upd) - set(gone)), 20, replace=False)]   # pre-update vectors
    return before, old, list(centers[:10])


def check_t3_stress(C: Checks) -> None:
    con = connect("smoke"); cur = con.cursor(); dim, n = 64, 20000
    out, lines = {}, []
    for label, versioned in (("versioned", True), ("plain control", False)):
        before, old, centers = _churned_table(cur, versioned, np.random.default_rng(7), n, dim)
        cur.execute("SELECT COUNT(*) FROM t3s FOR SYSTEM_TIME ALL" if versioned else "SELECT COUNT(*) FROM t3s")
        lines.append(f"{label}: {cur.fetchone()[0]} row-versions after 18,500 updates and 3,000 deletes")
        clause = f"FOR SYSTEM_TIME AS OF TIMESTAMP '{before}'" if versioned else ""
        k = _explain_keys(cur, f"SELECT id FROM t3s {clause} ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText(?)) LIMIT 30", (_vt(old[0]),))
        lines.append(f"{label}: EXPLAIN key of the {'AS OF ' if versioned else ''}ANN query: {sorted(x for x in k if x)}")
        for ef in (20, 80):
            cur.execute(f"SET mhnsw_ef_search = {ef}")
            if versioned:
                out[f"{label} AS OF, ef {ef}"] = _ann_vs_exact(cur, f"FOR SYSTEM_TIME AS OF TIMESTAMP '{before}'", old)
            out[f"{label} now, ef {ef}"] = _ann_vs_exact(cur, "", centers + old[:10])
    # backdated rows, as the loader writes them (system_versioning_insert_history)
    rng = np.random.default_rng(11); m = 8000
    V = [rng.normal(size=(m, dim)) for _ in range(3)]
    cur.execute("DROP TABLE IF EXISTS t3s"); cur.execute("SET system_versioning_insert_history = 1")
    cur.execute(f"CREATE TABLE t3s (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL, VECTOR INDEX (e) M=16 DISTANCE=cosine) WITH SYSTEM VERSIONING")
    for k, (s, e) in enumerate((("2010-01-01", "2013-01-01"), ("2013-01-01", "2016-01-01"), ("2016-01-01", None))):
        sql = "INSERT INTO t3s (id, e, row_start" + (", row_end) VALUES (?, VEC_FromText(?), ?, ?)" if e else ") VALUES (?, VEC_FromText(?), ?)")
        cur.executemany(sql, [(i, _vt(V[k][i]), s, e) if e else (i, _vt(V[k][i]), s) for i in range(m)])
    for ef in (20, 80):
        cur.execute(f"SET mhnsw_ef_search = {ef}")
        out[f"backdated AS OF 2011, ef {ef}"] = _ann_vs_exact(cur, "FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00'", list(V[0][:20]))
    cur.execute("DROP TABLE t3s")
    ver = {k: v for k, v in out.items() if not k.startswith("plain")}
    short, dups = sum(v["short"] for v in ver.values()), sum(v["dups"] for v in ver.values())
    vmin, ctl = min(v["min_recall"] for v in ver.values()), min(v["min_recall"] for k, v in out.items() if k.startswith("plain"))
    rev = _review_summary()
    got = (f"this run: {short} short sets, {dups} duplicate ids, min recall versioned {vmin} vs plain {ctl}; "
           f"review run: {rev}")
    C.add("F1.4-T3b", f"T3 at scale: {n:,} rows dim {dim}, churned; plus {m:,} ids x 3 backdated versions. ANN LIMIT 30 vs exact",
          "smoke", "outcome recorded (plan: may succeed, fail, or return fewer rows)", got, None,
          "\n".join(lines + [f"{k}: {v}" for k, v in out.items()]
                    + ["", "Adversarial review run (docs/validation/evidence/t3_review_*_20k.json, eval/repro/t3_review_lib.py):", rev]))
    C.finding(f"T3 at scale is intermittent. This run: {short} short result sets, min recall {vmin} on versioned tables vs {ctl} on "
              f"the plain control. Review run on the same server: {rev}. Decision: the plan's fallback is required, not optional. "
              f"Vectors live only in the plain `chunk` table and time travel is a join. F5 re-measures recall after the final load.")


def _review_summary() -> str:
    path = ROOT / "docs/validation/evidence/t3_review_versioned_20k.json"
    if not path.exists():
        return "no evidence file"
    rs = [r for r in json.loads(path.read_text(encoding="utf-8")) if r["k"] == 30 and r["snap"] == "asof_t0"]
    if not rs:
        return "evidence file has no AS OF LIMIT 30 rows"
    return (f"versioned AS OF, {sum(r['rows'] < r['exact_rows'] for r in rs)} of {len(rs)} LIMIT 30 queries short "
            f"(min {min(r['rows'] for r in rs)} rows), min recall {min(r['recall'] for r in rs):.2f}")


# ---------- T4: headline query with per-leg routing and an independent time oracle ----------
QUESTION = "Who was president of Finland from 1956 to 1982?"


def _states(K: int, W: int, M: int, P2: int, P3: int, F: int, fillers: list[str]) -> list[tuple]:
    s = [  # page, claim, text, start, end (None = current)
        (K, 1, "Urho Kekkonen oli maalaisliiton poliitikko ja pitkäaikainen pääministeri.", "2008-01-01 00:00:00", "2014-01-01 00:00:00"),
        (K, 1, "Urho Kekkonen toimi Suomen tasavallan presidenttinä vuosina 1956–1982.", "2014-01-01 00:00:00", None),
        (K, 2, "Kekkonen syntyi Pielavedellä vuonna 1900.", "2008-01-01 00:00:00", None),
        (K, 3, "Urho Kekkonen oli Suomen presidentti vuosina 1956–1982.", "2008-01-01 00:00:00", "2010-01-01 00:00:00"),  # dead, closest
        (W, 1, "Talvisota alkoi 30. marraskuuta 1939, kun Neuvostoliitto hyökkäsi Suomeen.", "2008-01-01 00:00:00", None),
        (M, 1, "Mannerheimin johdolla puolustusvoimat taistelivat talvisodassa ja jatkosodassa.", "2009-01-01 00:00:00", "2016-01-01 00:00:00"),
        (P2, 1, "Suomen tasavallan presidentti vuosina 1956–1982 johti maan ulkopolitiikkaa kylmän sodan aikana.", "2010-05-05 12:00:00.123456", None),
        (P3, 1, "Kajaanissa toimii Kekkonen-niminen kahvila, joka tunnetaan korvapuusteistaan.", "2011-06-01 00:00:00", "2019-01-01 00:00:00")]
    return s + [(F, 100 + i, t, "2001-01-01 00:00:00", "2002-01-01 00:00:00") for i, t in enumerate(fillers)]


def _leg(cur: mariadb.Cursor, sql: str, params: tuple) -> set:
    cur.execute(sql, params); return {r[0] for r in cur.fetchall()}


def check_t4(C: Checks) -> None:
    con = connect("smoke"); cur = con.cursor()
    cur.execute("SET system_versioning_insert_history = 1"); cur.execute("SET mhnsw_ef_search = 80")
    for t in ("claim", "chunk"):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    for stmt in split_sql((ROOT / "db/schema.sql").read_text()):          # the plan's DDL, verbatim
        if re.match(r"CREATE TABLE (chunk|claim) \(", stmt):
            cur.execute(stmt)
    qv = np.array(embed([QUESTION])[0]); qj = json.dumps(qv.tolist())
    r1_sql = ("SELECT page_id FROM (SELECT page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS d "
              "FROM wikirag.article_vector WHERE lang = 'fi' ORDER BY d LIMIT 12) AS a")
    r1 = _leg(cur, r1_sql, (qj,))
    cur.execute("SELECT title, page_id FROM wikirag.article WHERE lang='fi' AND title IN ('Urho Kekkonen','Talvisota','Carl Gustaf Emil Mannerheim')")
    pid = dict(cur.fetchall()); K, W, M = pid["Urho Kekkonen"], pid["Talvisota"], pid["Carl Gustaf Emil Mannerheim"]
    cur.execute(f"SELECT page_id FROM wikirag.article WHERE lang='fi' AND page_id NOT IN ({','.join(map(str, r1 | {K, W, M}))}) ORDER BY page_id LIMIT 3")
    P2, P3, F = [r[0] for r in cur.fetchall()]
    # >100 on-topic chunks, so R2's LIMIT 100 is selective and an off-topic chunk can only route through R3
    cur.execute("SELECT text FROM wikirag.article WHERE lang='fi' AND MATCH(title, text) AGAINST ('presidentti tasavallan presidentin') LIMIT 80")
    fillers = [p.strip() for (t,) in cur.fetchall() for p in t.split("\n")
               if 150 <= len(p.strip()) <= 900 and not p.lstrip().startswith(("#", "|", "-", "*"))][:220]
    assert len(fillers) >= 150, f"only {len(fillers)} filler paragraphs; R2 would not be selective"
    states = _states(K, W, M, P2, P3, F, fillers)
    texts = sorted({s[2] for s in states}); h = {t: i + 1 for i, t in enumerate(texts)}
    vec = {t: np.array(v) for t, v in zip(texts, embed(texts))}
    cur.executemany("INSERT INTO chunk (text_hash, text, emb) VALUES (?, ?, VEC_FromText(?))",
                    [(h[t], t, json.dumps(vec[t].tolist())) for t in texts])
    for p, c, t, s, e in states:
        cols, vals = "page_id, claim_id, text_hash, exact_hash, section, rev_id, editor, anon, row_start", [p, c, h[t], h[t], "lead", 1, "x", 0, s]
        if e:
            cols += ", row_end"; vals.append(e)
        cur.execute(f"INSERT INTO claim ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    headline = split_sql((ROOT / "db/headline.sql").read_text().replace("wikihistory.", "smoke."))[-1]
    r2 = _leg(cur, re.search(r"r2 AS \((.*?)\),\s*r3", headline, re.S)[1], (qj,))
    r3 = _leg(cur, re.search(r"r3 AS \((.*?)\),\s*routed", headline, re.S)[1], ("kekkonen*",))
    legs_ok = K in r1 and P2 in r2 - r1 - r3 and P3 in r3 - r1 - r2
    routed = r1 | r2 | r3
    # adapted copy that also returns the invisible period columns
    timed = (headline.replace("SELECT page_id, claim_id, text_hash, section, rev_id, editor",
                              "SELECT page_id, claim_id, text_hash, section, rev_id, editor, row_start, row_end")
                     .replace("SELECT s.page_id, s.claim_id,", "SELECT s.row_start, s.row_end, s.page_id, s.claim_id,"))
    dates = sorted({x for s in states[:8] for x in s[3:5] if x} | {"2005-01-01 00:00:00", "2012-03-01 00:00:00", "2020-01-01 00:00:00"})
    probes = [d for x in dates for d in (x, _shift(cur, x, -1), _shift(cur, x, 1))]
    viol = mism = n_rows = 0; per_date = {}
    dt = datetime.fromisoformat
    for t in probes:
        cur.execute(timed.replace("{as_of}", t), (qj, qj, "kekkonen*", qj)); rows = cur.fetchall(); n_rows += len(rows)
        viol += sum(not (r[0] <= dt(t) < r[1]) for r in rows)
        alive = [(p, c, x) for p, c, x, s, e in states if p in routed and dt(s) <= dt(t) and (e is None or dt(t) < dt(e))]
        want = [(p, c) for p, c, x in sorted(alive, key=lambda a: -float(qv @ vec[a[2]]) / np.linalg.norm(vec[a[2]]))[:8]]
        got = [(r[2], r[3]) for r in rows]; mism += got != want; per_date[t] = got
    cur.execute(headline.replace("{as_of}", "2012-03-01 00:00:00"), (qj, qj, "kekkonen*", qj)); verbatim = [(r[0], r[1]) for r in cur.fetchall()]
    keys = _explain_keys(cur, headline.replace("{as_of}", "2012-03-01 00:00:00"), (qj, qj, "kekkonen*", qj))
    ok = legs_ok and viol == 0 and mism == 0 and verbatim == per_date["2012-03-01 00:00:00"] and {"embedding", "emb"} <= keys
    C.add("F1.4-T4", "T4 headline query (verbatim) across schemas: each routing leg alone, AS OF vs an independent oracle",
          "invariant", "R1 routes Kekkonen; one page only via R2, one only via R3; 0 rows outside [row_start, row_end); "
          "ranked result = Python oracle at every boundary ±1 µs (≤ 8 live claims, so LIMIT 8 never cuts); EXPLAIN uses embedding (R1) and emb (R2)",
          f"legs ok {legs_ok}; {len(probes)} dates, {n_rows} rows, {viol} period violations, {mism} oracle mismatches; "
          f"index keys {sorted(k for k in keys if k)}", ok,
          f"R1={sorted(r1)}\nR2-only page {P2} in R2: {P2 in r2}, in R1/R3: {P2 in r1 | r3}\nR3-only page {P3} in R3: {P3 in r3}, "
          f"in R1/R2: {P3 in r1 | r2}\n{len(fillers)} filler chunks (dead claims on page {F})\n"
          + "\n".join(f"{t}: {v}" for t, v in per_date.items()))
    _r1_cost(C, cur, qj)
    C.finding("Headline query: `routed ... LIMIT 25` has no ORDER BY, so when the union exceeds 25 pages SQL drops pages "
              "arbitrarily before the API layer can trim by rank (adversarial review reproduced an R3-only page vanishing). "
              "F7 must rank or cap per leg inside SQL.")


def _shift(cur: mariadb.Cursor, ts: str, us: int) -> str:
    cur.execute("SELECT DATE_FORMAT(TIMESTAMP(?) + INTERVAL ? MICROSECOND, '%Y-%m-%d %H:%i:%s.%f')", (ts, us))
    return cur.fetchone()[0]


def _explain_keys(cur: mariadb.Cursor, sql: str, params: tuple) -> set:
    cur.execute("EXPLAIN " + sql, params)
    i = [d[0] for d in cur.description].index("key")
    return {r[i] for r in cur.fetchall()}


def _handler_reads(cur: mariadb.Cursor) -> int:
    cur.execute("SHOW SESSION STATUS WHERE Variable_name IN ('Handler_read_rnd', 'Handler_read_next', 'Handler_read_rnd_next')")
    return sum(int(v) for _, v in cur.fetchall())


def _r1_cost(C: Checks, cur: mariadb.Cursor, qj: str) -> None:
    out = {}
    for label, where in (("lang='fi' (as in the headline query)", "WHERE lang = 'fi'"), ("no filter", "")):
        before = _handler_reads(cur); t = time.perf_counter()
        cur.execute(f"SELECT page_id FROM (SELECT page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS d "
                    f"FROM wikirag.article_vector {where} ORDER BY d LIMIT 12) a", (qj,)); cur.fetchall()
        out[label] = (round((time.perf_counter() - t) * 1000), _handler_reads(cur) - before)
    C.add("F1.4-T4c", "R1 cost: vector index with and without the language filter", "smoke",
          "plan F7: R1 ~5 ms, hundreds of rows read", "; ".join(f"{k}: {ms} ms, {n} handler reads" for k, (ms, n) in out.items()), None)


# ---------- T5: embedding parity, and does Ollama today reproduce S's stored vectors? ----------
def check_t5(C: Checks) -> None:
    from sentence_transformers import SentenceTransformer
    cur = connect("wikirag").cursor()
    cur.execute("SELECT text FROM article WHERE lang = 'fi' ORDER BY page_id LIMIT 400")
    paras = [p.strip() for (t,) in cur.fetchall() for p in t.split("\n")
             if 150 <= len(p.strip()) <= 1500 and not p.startswith(("#", "|", "-"))]
    texts = paras[:: max(1, len(paras) // 50)][:50]
    embed(texts[:2]); t = time.perf_counter(); a = np.array(embed(texts)); t_ol = time.perf_counter() - t
    a = a / np.linalg.norm(a, axis=1, keepdims=True)
    m = SentenceTransformer("BAAI/bge-m3", device="mps"); m.encode(texts[:2])
    res = {}
    for seq in (8192, 512):                                       # 512 = the plan's F5 snippet: a truncation trap
        m.max_seq_length = seq; t = time.perf_counter()
        b = m.encode(texts, batch_size=16, normalize_embeddings=True); res[seq] = ((a * b).sum(axis=1), time.perf_counter() - t)
    cos = res[8192][0]
    C.add("F1.4-T5", "T5 Ollama vs sentence-transformers bge-m3 parity, 50 Finnish paragraphs (ST max_seq_length 8192)",
          "oracle", "min cosine >= 0.99 (plan); real parity ~0.9999", f"min {cos.min():.5f}, median {np.median(cos):.5f}",
          bool(cos.min() >= 0.99),
          f"ST max_seq_length 512 (plan's F5 snippet): min {res[512][0].min():.5f} — inputs over 512 tokens are cut\n"
          f"Ollama {len(texts) / t_ol:.1f} texts/s; sentence-transformers MPS fp32 {len(texts) / res[8192][1]:.1f} texts/s")
    cur.execute("SELECT a.page_id, v.truncated FROM article a JOIN article_vector v USING (lang, page_id) "
                "WHERE a.lang = 'fi' ORDER BY LENGTH(a.text), a.page_id")
    ids = cur.fetchall()
    chosen = [ids[int(q * (len(ids) - 1))][0] for q in np.linspace(0.05, 0.95, 8)] + [p for p, tr in ids if tr][-2:]
    cur.execute(f"SELECT a.title, a.text, v.truncated, VEC_ToText(v.embedding) FROM article a JOIN article_vector v "
                f"USING (lang, page_id) WHERE a.lang = 'fi' AND a.page_id IN ({','.join(map(str, chosen))})")
    pick = cur.fetchall()
    got = np.array(embed([f"{t}\n\n{x}" for t, x, _, _ in pick])); want = np.array([json.loads(r[3]) for r in pick])
    c2 = (got * want).sum(1) / np.linalg.norm(got, axis=1) / np.linalg.norm(want, axis=1)
    C.add("F1.4-T5b", "Ollama today reproduces S's stored vectors (10 fi articles by length, 2 truncated; title + text)",
          "oracle", "min cosine >= 0.999", f"min {c2.min():.6f}, median {np.median(c2):.6f}", bool(c2.min() >= 0.999),
          "\n".join(f"{len(x):>7} chars, truncated={bool(tr)}: {c:.6f}" for (_, x, tr, _), c in zip(pick, c2)))
    C.finding(f"Embeddings: Ollama {len(texts) / t_ol:.0f}/s vs sentence-transformers MPS {len(texts) / res[8192][1]:.0f}/s on 50 "
              f"paragraphs. ST parity is ~0.9999 only with max_seq_length >= the input; the plan's 512 gives min "
              f"{res[512][0].min():.4f} here. Use Ollama for every vector (it reproduces S to {c2.min():.6f}).")
