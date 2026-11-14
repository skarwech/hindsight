"""F1 — environment and smoke tests (plan section 7, F1; gate H2). Every number on the dashboard comes from here.

    .venv/bin/python eval/f1_check.py            # writes docs/validation/f1.json (~5 min)
"""
import csv, hashlib, json, pathlib, re, struct, subprocess, sys, time
from datetime import datetime, timedelta, timezone

import duckdb
import mariadb
import numpy as np
import pyarrow.parquet as pq

from common import ROOT, Checks, connect, embed, run_sql_file
from f1_vectors import check_t3_stress, check_t4, check_t5

RAG = ROOT / "starters" / "mariadb-vector-wikipedia-rag"
C = Checks("F1", "Environment and smoke tests", "H2")
# sha256 of each release's SHA256SUMS asset, read from the GitHub release pages (expanded_assets) on 2026-11-13
S_RELEASE_SUMS = "071e9929fcbecde313ca70ffe93f6b70d2221ccc218d693d69dd7ebed6aec8c7"   # s-2026-10
M_RELEASE_SUMS = "7d59a6a28b10f996be3c36fc4d82f5faa97624f8fca7a37cbf5d70e4bb809f68"   # m-2026-10


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# ---------- F1.1 checksums ----------
def check_checksums() -> None:
    m_sums = ROOT / "data/m/SHA256SUMS"
    out = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=m_sums.parent, capture_output=True, text=True).stdout
    n_ok, n_all = out.count(": OK"), len(m_sums.read_text().splitlines())
    tied = sha256(m_sums).startswith(M_RELEASE_SUMS)
    C.add("F1.1a", "Dataset M files match SHA256SUMS, and SHA256SUMS matches the release asset", "invariant",
          "17 of 17 OK; sums file = release digest", f"{n_ok} of {n_all} OK; sums file = release: {tied}",
          n_ok == n_all == 17 and tied, out)

    s_sums = ROOT / "data/s/SHA256SUMS"
    sums = dict(reversed(l.split()) for l in s_sums.read_text().splitlines())
    lines, ok = [f"S SHA256SUMS = release digest: {sha256(s_sums).startswith(S_RELEASE_SUMS)}"], sha256(s_sums).startswith(S_RELEASE_SUMS)
    for name, want in sorted(sums.items()):
        if not re.fullmatch(r"[\w.-]+", name):
            ok = False; lines.append(f"{name!r}: unexpected file name"); continue
        local = RAG / "data" / name
        if not local.exists():
            ok &= name == "README.md"                               # the only S file the starter does not ship
            lines.append(f"{name}: {'not used by the starter' if name == 'README.md' else 'MISSING'}"); continue
        if sha256(local) == want:
            lines.append(f"{name}: byte-identical to S"); continue
        ref = ROOT / "data/s" / name                               # re-encoded parquet? compare rows to the verified release file
        if not ref.exists() or sha256(ref) != want:
            ok = False; lines.append(f"{name}: bytes differ and no verified reference"); continue
        diff = duckdb.sql(f"SELECT (SELECT COUNT(*) FROM (FROM '{ref}' EXCEPT ALL FROM '{local}')) + "
                          f"(SELECT COUNT(*) FROM (FROM '{local}' EXCEPT ALL FROM '{ref}'))").fetchall()[0][0]
        n = duckdb.sql(f"SELECT COUNT(*) FROM '{ref}'").fetchall()[0][0]
        lines.append(f"{name}: bytes differ (re-encoded); {n} rows, all columns, {diff} rows differ"); ok &= diff == 0
    C.add("F1.1b", "RAG starter's copy of S equals the S release (s-2026-10)", "oracle",
          "every file byte-identical or row-identical to a verified release file", "identical" if ok else "DIFFERS", ok, "\n".join(lines))


# ---------- F1.2 counts and the loaded vectors ----------
def check_counts(cur: mariadb.Cursor) -> None:
    want = {"fi": 19285, "sv": 4921, "en": 5254}
    cur.execute("SELECT lang, COUNT(*) FROM article GROUP BY lang"); art = dict(cur.fetchall())
    cur.execute("SELECT lang, COUNT(*) FROM article_vector GROUP BY lang"); vec = dict(cur.fetchall())
    cur.execute("""SELECT (SELECT COUNT(*) FROM article a LEFT JOIN article_vector v USING (lang, page_id) WHERE v.page_id IS NULL),
                          (SELECT COUNT(*) FROM article_vector v LEFT JOIN article a USING (lang, page_id) WHERE a.page_id IS NULL),
                          (SELECT GROUP_CONCAT(DISTINCT model) FROM article_vector),
                          (SELECT SUM(truncated) FROM article_vector)""")
    no_vec, orphan, models, trunc = cur.fetchone()
    C.add("F1.2a", "S articles per language", "invariant", want, art, art == want)
    C.add("F1.2b", "S vectors per language, every article has one", "invariant",
          f"{want}, 0 missing, 0 orphans", f"{vec}, {no_vec} missing, {orphan} orphans", vec == want and no_vec == 0 and orphan == 0)
    C.add("F1.2c", "Vector model label and truncated articles", "oracle", "bge-m3; 290 truncated [S1]",
          f"{models}; {int(trunc)} truncated", models == "bge-m3" and int(trunc) == 290)
    # the loaded bytes, not just counts: every DB vector equals the release parquet vector
    bad, norms, n = 0, [], 0
    for lang in want:
        t = pq.read_table(RAG / f"data/vectors-bge-m3-{lang}.parquet", columns=["page_id", "embedding"]).to_pydict()
        ref = {p: hashlib.sha256(struct.pack(f"<{len(e)}f", *e)).hexdigest() for p, e in zip(t["page_id"], t["embedding"])}
        norms += [float(np.linalg.norm(e)) for e in t["embedding"][:: 50]]
        cur.execute("SELECT page_id, SHA2(embedding, 256), LENGTH(embedding) FROM article_vector WHERE lang = ?", (lang,))
        for p, h, ln in cur.fetchall():
            n += 1; bad += ref.get(p) != h or ln != 4096
    C.add("F1.2d", "Loaded vectors equal the release vectors byte for byte (1024 x float32)", "oracle",
          "29460 of 29460 equal; unit norms", f"{n - bad} of {n} equal; norms {min(norms):.5f}–{max(norms):.5f} (sample)",
          bad == 0 and n == 29460 and 0.999 < min(norms) <= max(norms) < 1.001)
    path = ROOT / "logs/s_load.log"
    log = path.read_text(encoding="utf-8") if path.exists() else ""
    m = re.search(r"cpu (\d+):([\d.]+) total", log)
    C.add("F1.2e", "S load time with the starter's setup.sh --local", "oracle", "about 3 min [S3]",
          f"{int(m[1])} min {float(m[2]):.0f} s" if m else "not logged", None, log)


# ---------- F1.3 starter baseline ----------
def _section(text: str, mode: str) -> str:
    return text.split(f"## {mode} search")[1].split("## ")[0] if f"## {mode} search" in text else ""


def _misses(section: str) -> list[str]:
    return [l[2:].split(", top result:")[0] for l in section.splitlines() if l.startswith("- [")]


def _table(section: str) -> list[tuple]:
    return re.findall(r"^\| ([^|]+?) \|\s*(\d+) \|\s*(\d+)% \|\s*(\d+)% \|\s*(\d+)% \| ([\d.]+) \|", section, re.M)


def check_baseline() -> None:
    py = str(RAG / ".venv/bin/python")
    target = subprocess.run([py, "-c", "from common import connect; c = connect().cursor(); "
                             "c.execute('SELECT VERSION(), @@port, DATABASE(), CURRENT_USER()'); print(*c.fetchone())"],
                            cwd=RAG, capture_output=True, text=True).stdout.strip()
    target = target or "UNKNOWN (connect failed)"
    t = time.time()
    out = subprocess.run([py, "evaluate.py", "--runs", "3", "--misses"], cwd=RAG, capture_output=True, text=True, timeout=900)
    text, ref = out.stdout + out.stderr, (RAG / "data/evaluation.txt").read_text(encoding="utf-8")
    on_target = target.startswith("12.3") and " 3306 wikirag " in f" {target} "
    for mode, cid, ref_all in (("vector", "F1.3a", (57, 76, 86, 0.65)), ("keyword", "F1.3b", (11, 16, 41, 0.15))):
        mine, theirs = _section(text, mode), _section(ref, mode)
        rows_m = _table(mine); all_m = next((r for r in rows_m if r[0] == "all"), None)
        same_rows, diff = rows_m == _table(theirs), sorted(set(_misses(mine)) ^ set(_misses(theirs)))
        g = (int(all_m[2]), int(all_m[3]), int(all_m[4]), float(all_m[5])) if all_m else None
        within = bool(g) and all(abs(a - b) <= 3 for a, b in zip(g[:3], ref_all[:3])) and round(abs(g[3] - ref_all[3]), 2) <= 0.03
        C.add(cid, f"Starter evaluate.py (ANN), {mode} search, 37 questions vs published", "oracle",
              f"plan F1: within 3 pp of {ref_all[0]}/{ref_all[1]}/{ref_all[2]}%, MRR {ref_all[3]}"
              + ("" if mode == "vector" else " (not gated in F1; F15's oracle)"),
              (f"{g[0]}/{g[1]}/{g[2]}%, MRR {g[3]}; every row identical: {same_rows}; misses differing: {len(diff)}") if g else "no output",
              (within and out.returncode == 0 and on_target) if mode == "vector" else None,   # keyword: recorded only
              f"evaluate.py exit {out.returncode}, connected to: {target}\n\nmisses only on one side:\n" + "\n".join(diff) + "\n\n" + text)
        if diff:
            C.finding(f"{mode} baseline (ANN, published on MariaDB 12.2.2, here {target.split()[0]}) differs from the published "
                      f"miss list in this run: " + "; ".join(diff))
    C.finding(f"evaluate.py ran in {time.time() - t:.0f} s against {target}.")


def check_exact_baseline() -> None:
    """Deterministic oracle: the starter's 37 questions with an exact scan (IGNORE INDEX) must give the published numbers."""
    cur = connect("wikirag").cursor(); cur.execute("SET mhnsw_ef_search = 80")
    with open(RAG / "data/test-questions.tsv", encoding="utf-8") as f:
        qs = list(csv.DictReader(f, delimiter="\t"))
    sql = """SELECT a.lang, a.page_id, a.wikidata_id FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS d
             FROM article_vector {hint} ORDER BY d LIMIT 10) v JOIN article a USING (lang, page_id) ORDER BY v.d"""
    ranks, flips = {"exact": [], "ann": []}, []
    for q in qs:
        cur.execute("SELECT wikidata_id, page_id FROM article WHERE lang = 'fi' AND title = ?", (q["expected_fi_title"],))
        wd, pid = cur.fetchone(); v = json.dumps(embed([q["question"]])[0])
        for kind, hint in (("exact", "IGNORE INDEX (embedding)"), ("ann", "")):
            cur.execute(sql.format(hint=hint), (v,))
            ranks[kind].append(next((i + 1 for i, (l, p, w) in enumerate(cur.fetchall())
                                     if (wd and w == wd) or (l == "fi" and p == pid)), None))
        if ranks["exact"][-1] != ranks["ann"][-1]:
            flips.append(f"[{q['lang']}] {q['question']}: exact rank {ranks['exact'][-1]}, ANN rank {ranks['ann'][-1]}")
    def summary(rs: list[int | None]) -> tuple[int, int, int, float]:
        hit = lambda k: round(100 * sum(1 for r in rs if r and r <= k) / len(rs))
        return hit(1), hit(5), hit(10), round(sum(1 / r for r in rs if r) / len(rs), 2)
    ex, an = summary(ranks["exact"]), summary(ranks["ann"])
    C.add("F1.3c", "Exact search (IGNORE INDEX) on the starter's 37 questions = published numbers (deterministic oracle)", "oracle",
          "57/76/86%, MRR 0.65 exactly", f"exact {ex[0]}/{ex[1]}/{ex[2]}%, MRR {ex[3]}; ANN ef 80 now {an[0]}/{an[1]}/{an[2]}%, MRR {an[3]}; "
          f"{len(flips)} questions where ANN rank != exact rank", ex == (57, 76, 86, 0.65), "\n".join(flips) or "ANN = exact on all 37")


# ---------- F1.4 smoke tests (SQL) ----------
PITFALLS = [  # (label prefix in col 1, expected col 2); each must also return exactly one row
    ("T1 mid", "old"), ("T1 at boundary", "new"), ("T1 boundary-1us", "old"), ("T1 before first", 0),
    ("LIFE 2011-06", "7,8"), ("LIFE 2012-06 gap", "8"), ("LIFE 2013-06 revived", "7,8"),
    ("LIFE 2014-06", "7:222,8:333"), ("LIFE 2016", "7"), ("LIFE current rows per PK", 0),
    ("ZERO-LEN accepted?", 0), ("ALIAS after clause", 8), ("LIMIT wrapped", "1"), ("CHAIN broken", 0),
    ("KEEP: rows", "2,0"), ("KEEP: periods", "2010-01-01|2012-01-01"), ("secure_timestamp", "NO")]
PITFALL_ERRORS = [  # (sql fragment, accepted error codes, code on 10.11) — None = must be accepted
    ("'2012-01-01 00:00:00','2012-01-01 00:00:00'", {1525, 4112}, 1525), ("FROM c AS k FOR SYSTEM_TIME", {1064}, 1064),
    ("SELECT 'LIMIT direct'", {1235}, 1235), ("CREATE TEMPORARY TABLE", {4137}, 4137),
    ("ALTER TABLE c ADD COLUMN extra", {4119}, 4119), ("ALTER TABLE c MODIFY COLUMN extra", {4119}, 4119),
    ("ALTER TABLE px MODIFY COLUMN v", {4119}, 4119),
    ("ALTER TABLE c ADD KEY (rev_id)", None, None), ("ALTER TABLE px ADD COLUMN extra", None, None)]


def labelled(res: list[dict], prefix: str) -> list[dict]:
    return [r for r in res if r["rows"] and str(r["rows"][0][0]).startswith(prefix)]


def _pitfall_lines(res: list[dict]) -> list[tuple[bool, str]]:
    out = []
    for prefix, want in PITFALLS:
        r = labelled(res, prefix); got = r[0]["rows"][0][1] if r else "missing"; nrows = len(r[0]["rows"]) if r else 0
        out.append((got == want and nrows == 1, f"{prefix}: {got} ({nrows} row) — expected {want}"))
    for frag, codes, old in PITFALL_ERRORS:
        r = [x for x in res if frag in x["sql"]]; e = r[0]["error"] if r else ["missing", ""]
        ok = (e is None) if codes is None else bool(e) and e[0] in codes
        out.append((ok, f"{frag}: {'accepted' if not e else 'ERROR ' + str(e[0])} — expected {'accepted' if codes is None else codes}"))
        if ok and e and old and e[0] != old:
            C.finding(f"12.3 rejects `{frag}` with error {e[0]}; the plan's 10.11 sandbox saw {old}: {e[1]}")
    expected = [frag for frag, codes, _ in PITFALL_ERRORS if codes]
    unexpected = [r["sql"][:70] + f" -> {r['error'][0]}" for r in res if r["error"] and not any(f in r["sql"] for f in expected)]
    out.append((not unexpected, f"unexpected SQL errors: {unexpected or 0}"))
    flips = {r[1]: r[2] for r in labelled(res, "FLIPS")[0]["rows"]} if labelled(res, "FLIPS") else {}
    cte = [r[1] for r in labelled(res, "CTE ok")[0]["rows"]] if labelled(res, "CTE ok") else []
    prep = [r["rows"][0][0] for r in res if r["sql"].startswith("EXECUTE s")]
    ub, ua = labelled(res, "UPD before")[0]["rows"][0], labelled(res, "UPD after")[0]["rows"][0]
    fresh = abs(datetime.fromisoformat(str(ua[2])) - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(minutes=5)
    out += [(flips == {20: 3, 21: 1}, f"FLIPS: {flips} — expected {{20: 3, 21: 1}}"),
            (cte == [8, 7], f"CTE + AS OF + IN + join: {cte} — expected [8, 7]"),
            (prep == [2, 2], f"SQL PREPARE, bare date and full datetime: {prep} — expected [2, 2]"),
            (ua[1] == ub[1] + 1 and fresh, f"UPDATE writes a history row stamped now: {ub[1]} -> {ua[1]} rows, start {ua[2]}")]
    return out


def check_pitfalls(con: mariadb.Connection) -> None:
    con.cursor().execute("SET system_versioning_insert_history = 1")
    res = run_sql_file(con, ROOT / "db/smoke/pitfalls.sql") + run_sql_file(con, ROOT / "db/smoke/pitfalls_extra.sql")
    lines = _pitfall_lines(res)
    t1 = [(ok, l) for ok, l in lines if l.startswith("T1 ")]
    C.add("F1.4-T1", "T1 backdated history rows as an ordinary user; AS OF boundaries (exactly one row each)", "invariant",
          "old / new / old / 0", " / ".join(l.split(": ")[1].split(" (")[0] for _, l in t1), all(ok for ok, _ in t1),
          "\n".join(l for _, l in t1))
    bad = sum(not ok for ok, _ in lines)
    C.add("F1.4-P", "pitfalls.sql (Appendix C) + pitfalls_extra.sql on 12.3: values, row counts and error codes", "oracle",
          f"{len(lines)} of {len(lines)} as expected", f"{len(lines) - bad} of {len(lines)} as expected", bad == 0,
          "\n".join(("ok  " if ok else "BAD ") + l for ok, l in lines))
    cur = con.cursor()                                           # how the app will pass AS OF: a ? parameter
    out = []
    for ts in ("2011-06-01", "2011-06-01 00:00:00"):
        try:
            cur.execute("SELECT COUNT(*) FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP ?", (ts,)); out.append(f"{ts!r}: n={cur.fetchone()[0]}")
        except mariadb.Error as e:
            out.append(f"{ts!r}: ERROR {e.errno}")
    C.add("F1.4-P2", "AS OF TIMESTAMP ? through the Python connector: bare date vs full datetime", "smoke",
          "full datetime works (plan: always pass 'YYYY-MM-DD HH:MM:SS')", "; ".join(out), out[1].endswith(": n=1"))


def check_t2_t3(con: mariadb.Connection) -> None:
    res = run_sql_file(con, ROOT / "db/smoke/t2_t3.sql")
    errors = [r for r in res if r["error"]]
    get = lambda p: labelled(res, p)[0]["rows"][0][1] if labelled(res, p) else "missing"
    explain = [r for r in res if r["sql"].startswith("EXPLAIN")]
    key = lambda i: dict(zip(explain[i]["cols"], explain[i]["rows"][0])).get("key") if len(explain) > i else None
    t2 = (get("T2 MATCH"), get("T2 VECTOR"), key(0))
    C.add("F1.4-T2", "T2 HNSW + FULLTEXT on one table (4 rows): MATCH and vector query", "invariant",
          "MATCH 1,2; vector top-2 1,2; EXPLAIN uses index e", f"MATCH {t2[0]}; vector {t2[1]}; index {t2[2]}",
          t2 == ("1,2", "1,2", "e") and not errors, "\n".join(f"{r['sql'][:90]} -> {r['rows'] or r['error']}" for r in res))
    t3 = [get(p) for p in ("T3 all row-versions", "T3 current, ANN LIMIT 5", "T3 current, ANN LIMIT 15",
                           "T3 AS OF before, LIMIT 5", "T3 AS OF before, LIMIT 20", "T3 AS OF before, exact")]
    C.add("F1.4-T3", "T3 HNSW on a system-versioned table creates and answers (toy: 20 rows, so the graph is fully visited)",
          "smoke", "creates; LIMIT k returns k now and AS OF; ANN = exact; EXPLAIN uses e now and AS OF; no SQL errors",
          f"{t3}; index now {key(1)}, AS OF {key(2)}; errors {len(errors)}",
          t3 == [28, 5, 15, 5, 20, 1] and key(1) == "e" and key(2) == "e" and not errors)


# ---------- F1.5 server ----------
def check_server(cur: mariadb.Cursor) -> None:
    cur.execute("SELECT VERSION(), @@max_allowed_packet, CURRENT_USER(), @@secure_timestamp")
    ver, packet, user, secure = cur.fetchone()
    major, minor = map(int, ver.split(".")[:2])
    cur.execute("SET SESSION system_versioning_insert_history = 1"); cur.execute("SELECT @@system_versioning_insert_history")
    ih = cur.fetchone()[0]
    cur.execute("SHOW GRANTS")
    grants = [re.sub(r" IDENTIFIED BY PASSWORD '[^']*'", " IDENTIFIED BY PASSWORD <redacted>", g[0]) for g in cur.fetchall()]
    ordinary = not any("ON *.*" in g and "USAGE" not in g for g in grants)
    C.add("F1.5", "Server: version >= 11.7, insert_history by an ordinary user, max_allowed_packet >= 64 MB", "invariant",
          ">= 11.7; 1 as non-root; >= 64 MB", f"{ver}; {ih} as {user} (ordinary: {ordinary}, secure_timestamp {secure}); {packet >> 20} MB",
          (major, minor) >= (11, 7) and ih == 1 and ordinary and packet >= 64 << 20, "\n".join(grants))


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    def reset_smoke() -> None:
        cur = connect("smoke").cursor(); cur.execute("DROP DATABASE IF EXISTS smoke")
        cur.execute("CREATE DATABASE smoke CHARACTER SET utf8mb4")
    steps = [reset_smoke, check_checksums, lambda: check_counts(connect("wikirag").cursor()), check_baseline, check_exact_baseline, lambda: check_pitfalls(connect("smoke")),
             lambda: check_t2_t3(connect("smoke")), lambda: check_t3_stress(C), lambda: check_t4(C), lambda: check_t5(C),
             lambda: check_server(connect("wikirag").cursor())]
    for i, step in enumerate(steps):
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"F1.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
