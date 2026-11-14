"""F2 — corpus layer page_state and triage (plan section 7, F2; gate H5).

    .venv/bin/python eval/f2_check.py            # writes docs/validation/f2.json (~1 min)
"""
import csv, json, random, re, subprocess, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import mariadb
import requests

from common import ROOT, Checks, connect

sys.path.insert(0, str(ROOT / "pipeline"))
from revisions import flags  # noqa: E402

C = Checks("F2", "Corpus layer page_state and triage", "H5")
SVT = ROOT / "starters/mariadb-system-versioning-wikipedia"
M_FILES = str(ROOT / "data/m/versions-*.parquet")
ANON_RE = r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$|^[0-9a-fA-F]*:[0-9a-fA-F:]+$|^~[0-9]{4}-"   # the starter's undid_anonymous regex
STARTER_REVERTS = """SELECT SUM(r), SUM(r AND u) FROM (
  SELECT sha1 <> LAG(sha1) OVER (ORDER BY row_start) AND MIN(row_start) OVER (PARTITION BY sha1) < row_start AS r,
         LAG(editor) OVER (ORDER BY row_start) REGEXP ? AS u
  FROM page_state FOR SYSTEM_TIME ALL WHERE page_id = ?) x"""


def tsv(path: "Path") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def parquet_rows() -> dict[int, tuple]:
    d = duckdb.connect()
    rows = d.execute("""SELECT rev_id, page_id, title, "user", COALESCE(anonymous, false), COALESCE(minor, false), comment, sha1, bytes,
                         timezone('UTC', "timestamp") FROM read_parquet(?)""", [M_FILES]).fetchall()
    return {r[0]: r[1:] for r in rows}


def db_rows(cur: mariadb.Cursor) -> dict[int, tuple]:
    cur.execute("""SELECT rev_id, page_id, title, editor, anon, minor, comment, sha1, bytes, row_start, row_end, is_revert, undone
                   FROM page_state FOR SYSTEM_TIME ALL""")
    return {r[0]: r[1:] for r in cur.fetchall()}


# ---------- F2.1 counts and row identity ----------
def check_counts(cur: mariadb.Cursor, pq: dict, db: dict) -> None:
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT page_id) FROM page_state FOR SYSTEM_TIME ALL"); n, pages = cur.fetchone()
    C.add("F2.1a", "All versions and pages loaded", "invariant", "862,076 versions, 19,742 pages", f"{n:,} versions, {pages:,} pages",
          n == 862076 and pages == 19742)
    per_pq, per_db = Counter(v[0] for v in pq.values()), Counter(v[0] for v in db.values())
    bad_pages = [p for p in per_pq.keys() | per_db.keys() if per_pq[p] != per_db[p]]
    C.add("F2.1b", "Per-page version counts equal the Parquet counts", "invariant", "0 mismatching pages",
          f"{len(bad_pages)} mismatching pages of {len(per_pq):,}", not bad_pages, bad_pages[:20])
    fields = ("page_id", "title", "editor", "anon", "minor", "comment", "sha1", "bytes")
    groups = defaultdict(list)
    for rev, p in pq.items():
        groups[(p[0], p[8])].append(rev)
    want = {rev: timedelta(microseconds=k) for revs in groups.values() for k, rev in enumerate(sorted(revs))}
    diff = Counter()
    for rev, p in pq.items():
        d = db.get(rev)
        if d is None:
            diff["missing"] += 1; continue
        for i, f in enumerate(fields):
            diff[f] += (bool(p[i]) != bool(d[i])) if f in ("anon", "minor") else (p[i] != d[i])
        diff["row_start"] += d[8] - p[8] != want[rev]
    n_bumped = sum(1 for v in want.values() if v)
    C.add("F2.1c", "Every Parquet row is in page_state with identical metadata; row_start = edit time + the expected µs bump",
          "oracle", "0 mismatches; same-second edits shifted by +k µs in rev_id order (expected from the Parquet)",
          f"{sum(diff.values())} mismatches {dict(+diff) or ''}; {n_bumped} rows needed a bump",
          sum(diff.values()) == 0 and n_bumped > 0)


# ---------- F2.2 chain ----------
def check_chain(cur: mariadb.Cursor) -> None:
    cur.execute("""WITH h AS (
                     SELECT page_id, row_start, row_end,
                            LEAD(row_start) OVER (PARTITION BY page_id ORDER BY row_start) AS next_start
                     FROM page_state FOR SYSTEM_TIME ALL)
                   SELECT COUNT(*) FROM h WHERE row_end <= row_start OR (next_start IS NOT NULL AND row_end <> next_start)""")
    broken = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) - COUNT(DISTINCT page_id) FROM page_state"); extra = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT page_id FROM page_state FOR SYSTEM_TIME ALL GROUP BY page_id "
                "HAVING MAX(row_end) < TIMESTAMP'2038-01-01 00:00:00') x"); no_current = cur.fetchone()[0]
    C.add("F2.2", "History chain: each version ends exactly where the next begins; one current row per page", "invariant",
          "0 broken; 0 extra current rows; 0 pages without a current row",
          f"{broken} broken; {extra} extra current rows; {no_current} pages without a current row",
          broken == 0 and extra == 0 and no_current == 0)


# ---------- F2.3 point-in-time identity ----------
def check_point_in_time(cur: mariadb.Cursor, db: dict) -> None:
    by_page = defaultdict(list)
    for rev, d in db.items():
        by_page[d[0]].append((d[8], rev))
    for v in by_page.values():
        v.sort()
    rnd = random.Random(2026); sample = rnd.sample(sorted(db), 10000)
    bad_at, bad_before, lat = [], [], []
    sql = "SELECT rev_id FROM page_state FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?"
    for rev in sample:
        page, start = db[rev][0], db[rev][8]
        vs = by_page[page]; i = vs.index((start, rev))
        t = time.perf_counter(); cur.execute(sql, (start.strftime("%Y-%m-%d %H:%M:%S.%f"), page)); got = cur.fetchall()
        lat.append((time.perf_counter() - t) * 1000)
        if [r[0] for r in got] != [rev]:
            bad_at.append(rev)
        cur.execute(sql, ((start - timedelta(microseconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f"), page))
        want = [vs[i - 1][1]] if i else []
        if [r[0] for r in cur.fetchall()] != want:
            bad_before.append(rev)
    lat.sort()
    C.add("F2.3", "AS OF a revision's own time returns it; 1 µs earlier returns its predecessor (10,000 random revisions)",
          "invariant", "10,000 of 10,000 both ways", f"{10000 - len(bad_at):,} at; {10000 - len(bad_before):,} at -1 µs",
          not bad_at and not bad_before, f"failures at: {bad_at[:10]}\nfailures before: {bad_before[:10]}")
    C.add("F2.3b", "AS OF latency, one page (10,000 timed queries)", "smoke", "plan F6: p95 <= 10 ms (for claims)",
          f"p50 {lat[len(lat) // 2]:.2f} ms, p95 {lat[int(len(lat) * .95)]:.2f} ms", None)


# ---------- F2.4 oracle: the starter's 50 articles from the same dump ----------
def check_starter(cur: mariadb.Cursor, db: dict) -> None:
    ids = {int(a["page_id"]): a["title"] for a in tsv(SVT / "data/articles.tsv")}
    per_year = Counter(d[8].year for d in db.values() if d[0] in ids)
    total = sum(per_year.values())
    txt = (SVT / "data/measure.txt").read_text(encoding="utf-8")
    theirs = {int(y): int(n.replace(",", "")) for y, n in re.findall(r"^\| (\d{4}) \| ([\d,]+) \|", txt, re.M)}
    year_bad = {y: (per_year[y], theirs[y]) for y in theirs if per_year[y] != theirs[y]}
    count = lambda title: sum(1 for d in db.values() if d[0] in ids and ids[d[0]] == title)
    sis, talvi = count("Suomen sisällissota"), count("Talvisota")
    C.add("F2.4a", "Starter's 50 articles: total versions, per year (measure.txt), and the two largest", "oracle",
          "31,848; all 24 years equal; Suomen sisällissota 3,357; Talvisota 2,143",
          f"{total:,}; {len(theirs) - len(year_bad)} of {len(theirs)} years equal; {sis:,}; {talvi:,}",
          total == 31848 and len(theirs) == 24 and not year_bad and sis == 3357 and talvi == 2143, year_bad or "all years equal")
    C.table("Starter's 50 articles: versions per year, page_state vs the starter's measure.txt", ["Year", "page_state", "starter"],
            [[str(y), per_year[y], theirs[y]] for y in sorted(theirs)])
    talvi_id = next(p for p, t in ids.items() if t == "Talvisota")
    cur.execute(STARTER_REVERTS, (ANON_RE, talvi_id)); r, u = map(int, cur.fetchone())
    ours = sum(1 for d in db.values() if d[0] == talvi_id and d[10])
    cur.execute("""SELECT SUM(is_revert AND pa) FROM (SELECT is_revert, LAG(anon) OVER (ORDER BY row_start) AS pa
                   FROM page_state FOR SYSTEM_TIME ALL WHERE page_id = ?) x""", (talvi_id,)); flag_anon = int(cur.fetchone()[0])
    C.add("F2.4b", "Talvisota reverts (starter's window SQL and our load-time flag) and how many undo an anonymous edit",
          "oracle", "328 reverts both ways; 240 undo an anonymous editor (starter's regex)",
          f"starter SQL {r}, our is_revert {ours}; {u} by the starter's regex, {flag_anon} by M's anonymous flag",
          r == 328 and ours == 328 and u == 240)
    if flag_anon != u:
        C.finding(f"Talvisota: {u} reverts undo an anonymous editor by the starter's IP/temporary-account regex, {flag_anon} by "
                  f"dataset M's `anonymous` flag. The flag also covers anonymous editors stored as host names in early dumps.")


# ---------- F2.5 / F2.6 sanity and revert flags ----------
def check_flags(cur: mariadb.Cursor, db: dict) -> None:
    anon = sum(bool(d[3]) for d in db.values()) / len(db)
    C.add("F2.5", "Anonymous share of versions", "oracle", "17–21% (dataset README: 19%)", f"{anon:.1%}", 0.17 <= anon <= 0.21)
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_revisions.py"], cwd=ROOT / "pipeline", capture_output=True, text=True)
    m = re.search(r"(\d+) passed", out.stdout); passed = int(m[1]) if m else 0
    C.add("F2.6a", "Golden cases for is_revert / undone / bump / NULL sha1 (pipeline/test_revisions.py)", "golden",
          "7 of 7 pass", f"{passed} passed, exit {out.returncode}", out.returncode == 0 and passed == 7, out.stdout[-2000:])
    cur.execute("""SELECT COUNT(*), SUM(is_revert), SUM(COALESCE(w, 0)) FROM (
                     SELECT is_revert, sha1 <> LAG(sha1) OVER (PARTITION BY page_id ORDER BY row_start)
                            AND MIN(row_start) OVER (PARTITION BY page_id, sha1) < row_start AS w
                     FROM page_state FOR SYSTEM_TIME ALL) x WHERE is_revert <> COALESCE(w, 0)""")
    mism, _, _ = cur.fetchone()
    cur.execute("SELECT SUM(is_revert), SUM(undone), SUM(undone AND anon) FROM page_state FOR SYSTEM_TIME ALL")
    rev, und, und_anon = map(int, cur.fetchone())
    by_page = defaultdict(list)
    for d in db.values():
        by_page[d[0]].append(d)
    bad_rev = bad_und = 0
    for vs in by_page.values():
        vs.sort(key=lambda d: d[8])
        r, u = flags([d[6] for d in vs], [d[8] for d in vs])
        bad_rev += sum(bool(d[10]) != x for d, x in zip(vs, r)); bad_und += sum(bool(d[11]) != x for d, x in zip(vs, u))
    C.add("F2.6b", "Stored is_revert = the starter's window-function rule; stored is_revert and undone = flags() re-run on the stored rows",
          "invariant", "0 of 862,076 differ on each; reverts > 0 and undone > 0",
          f"SQL rule: {mism} differ; flags() re-run: {bad_rev} is_revert, {bad_und} undone differ; {rev:,} reverts, "
          f"{und:,} undone ({und_anon / max(und, 1):.0%} of them anonymous)",
          mism == 0 and bad_rev == 0 and bad_und == 0 and rev > 0 and und > 0)


# ---------- F2.7 live Wikipedia ----------
def b36(hexdigest: str) -> str:
    n, out = int(hexdigest, 16), ""
    while n:
        n, r = divmod(n, 36); out = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + out
    return out.rjust(31, "0")


def check_live(db: dict, pq: dict) -> None:
    """Ten revisions chosen to cover the edge cases: 2003, 2026, an IP edit, a temporary account, a minor edit,
    a same-second (bumped) edit, a hidden-text edit (NULL sha1), and three random ones. One API request."""
    rnd = random.Random(7)
    pick = lambda cond: rnd.choice(sorted(r for r, v in pq.items() if cond(r, v)))
    cases = {"2003": pick(lambda r, v: v[8].year == 2003),
             "2026 (before 1 Oct)": pick(lambda r, v: v[8] < datetime(2026, 10, 1) and v[8].year == 2026),
             "IP edit": pick(lambda r, v: v[3] and v[2] and not v[2].startswith("~")),
             "temporary account": pick(lambda r, v: v[2] and v[2].startswith("~2")),
             "minor edit": pick(lambda r, v: v[4]),
             "same-second (bumped)": pick(lambda r, v: db[r][8] != v[8]),
             "hidden text (NULL sha1)": pick(lambda r, v: v[6] is None)}
    for i in range(3):
        cases[f"random {i + 1}"] = pick(lambda r, v: v[8] < datetime(2026, 10, 1))
    r = requests.get("https://fi.wikipedia.org/w/api.php", timeout=30, headers={"User-Agent": "Hindsight-validation/0.1 (hackathon research script)"},
                     params=dict(action="query", prop="revisions", revids="|".join(map(str, cases.values())),
                                 rvprop="ids|timestamp|user|sha1|size|flags", format="json", formatversion=2))
    r.raise_for_status()
    live = {rv["revid"]: (p["pageid"], rv) for p in r.json()["query"]["pages"] for rv in p.get("revisions", [])}
    lines, ok = [], 0
    for label, rev in cases.items():
        d, src = db[rev], pq[rev]
        if rev not in live:
            lines.append(f"{label} {rev}: not returned by the API"); continue
        pid, rv = live[rev]
        same = dict(page=pid == d[0], time=rv["timestamp"] == src[8].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    user=(d[2] is None) if rv.get("userhidden") else rv.get("user") == d[2],
                    sha1=(d[6] is None) if rv.get("sha1hidden") else b36(rv["sha1"]) == d[6],
                    size=rv.get("size") == d[7], minor=bool(rv.get("minor")) == bool(d[4]))
        ok += all(same.values())
        lines.append(f"{label} {rev} ({src[8]:%Y-%m-%d}): " + ", ".join(f"{k} {'ok' if v else 'DIFF'}" for k, v in same.items())
                     + (" [hidden on wiki: " + ",".join(k for k in ("userhidden", "sha1hidden", "texthidden") if rv.get(k)) + "]"
                        if any(rv.get(k) for k in ("userhidden", "sha1hidden", "texthidden")) else ""))
    C.add("F2.7", "Ten revisions covering the edge cases match live fi.wikipedia: page, UTC time, user, sha1, size, minor", "oracle",
          f"{len(cases)} of {len(cases)}", f"{ok} of {len(cases)}", ok == len(cases), "\n".join(lines))


def _bot(e: str) -> bool:
    return e.lower().endswith("bot") or e == "CommonsDelinker"


def check_triage_recompute(db: dict) -> None:
    """Independent Python re-implementation of the strict dispute rule and edit-war months, vs the frozen hero.tsv."""
    hero = {int(h["page_id"]): h for h in tsv(ROOT / "docs/hero.tsv")}
    by_page = defaultdict(list)
    for d in db.values():
        if d[0] in hero:
            by_page[d[0]].append(d)
    bad = []
    for pid, vs in by_page.items():
        vs.sort(key=lambda d: d[8]); months = Counter()
        for prev, d in zip(vs, vs[1:]):
            if (d[10] and not d[3] and not prev[3] and d[2] and prev[2] and d[2] != prev[2]
                    and not _bot(d[2]) and not _bot(prev[2])):
                months[d[8].strftime("%Y-%m")] += 1
        got = (sum(months.values()), sum(1 for n in months.values() if n >= 3), len(vs))
        want = (int(hero[pid]["disputes"]), int(hero[pid]["war_months"]), int(hero[pid]["versions"]))
        if got != want:
            bad.append(f"{hero[pid]['title']}: python {got} vs hero.tsv {want}")
    C.add("F2.8b", "Triage numbers in hero.tsv reproduce from an independent Python implementation (disputes, war months, versions)",
          "oracle", f"{len(hero)} of {len(hero)} pages equal", f"{len(hero) - len(bad)} of {len(hero)} equal",
          not bad and len(by_page) == len(hero), "\n".join(bad) or "all equal")


# ---------- hero set, load and triage numbers ----------
def check_hero_and_costs(cur: mariadb.Cursor) -> None:
    gold = {q["expected_fi_title"] for q in tsv(ROOT / "starters/mariadb-vector-wikipedia-rag/data/test-questions.tsv")}
    hero = tsv(ROOT / "docs/hero.tsv")
    titles = {h["title"] for h in hero}
    starter = {int(a["page_id"]) for a in tsv(SVT / "data/articles.tsv")}
    hero_ids = {int(h["page_id"]) for h in hero}
    cur.execute(f"SELECT COUNT(*) FROM page_state WHERE page_id IN ({','.join(map(str, sorted(hero_ids)))})"); in_db = cur.fetchone()[0]
    C.add("F2.8", "Hero set frozen (docs/hero.tsv + hero.md): all gold pages of the 37 test questions and the starter's 50, all in page_state",
          "invariant", f"{len(gold)} of {len(gold)} gold titles; 50 of 50 starter pages; every hero page in page_state",
          f"{len(gold & titles)} of {len(gold)} gold; {len(starter & hero_ids)} of 50 starter; {in_db} of {len(hero)} pages in page_state",
          gold <= titles and starter <= hero_ids and in_db == len(hero) and (ROOT / "docs/hero.md").exists(), sorted(gold - titles))
    load = json.loads((ROOT / "logs/page_state_load.json").read_text(encoding="utf-8"))
    tri = json.loads((ROOT / "logs/triage.json").read_text(encoding="utf-8"))
    cur.execute("SELECT ROUND((data_length + index_length) / 1048576) FROM information_schema.tables "
                "WHERE table_schema = 'wikihistory' AND table_name = 'page_state'"); mb = int(cur.fetchone()[0])
    C.add("F2.9", "Load time, size, triage latency", "smoke",
          "plan est.: load tens of minutes; a few hundred MB; triage in seconds",
          f"load {load['seconds']} s ({load['workers']} workers, fresh {load['fresh']}); {mb} MB; triage {tri['ms'] / 1000:.1f} s over {tri['pages']:,} pages", None,
          json.dumps({k: v for k, v in load.items() if k != "files"}))
    C.table("Triage: most contested pages (rank-average of disputes per 100 versions and edit-war months)",
            ["#", "Page", "Versions", "Disputes /100", "Edit-war months"],
            [[str(i), p["title"], p["versions"], p["disputes_per_100"], p["war_months"]] for i, p in enumerate(tri["top"][:12], 1)])


def check_resume(cur: mariadb.Cursor) -> None:
    cur.execute("SELECT COUNT(*) FROM page_state FOR SYSTEM_TIME ALL"); before = cur.fetchone()[0]
    out = subprocess.run([sys.executable, str(ROOT / "pipeline/load_page_state.py")], capture_output=True, text=True, timeout=600)
    stats = json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 else {}
    cur.execute("SELECT COUNT(*) FROM page_state FOR SYSTEM_TIME ALL"); after = cur.fetchone()[0]
    C.add("F2.10", "Resume: re-running the loader on a full table inserts nothing", "invariant",
          "0 pages loaded, 19,742 skipped, row count unchanged",
          f"{stats.get('pages')} loaded, {stats.get('skipped')} skipped; rows {before:,} -> {after:,}",
          stats.get("pages") == 0 and stats.get("skipped") == 19742 and before == after, out.stdout[-1000:] + out.stderr[-1000:])


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state: dict = {}
    steps = [lambda: state.update(cur=connect("wikihistory").cursor()),
             lambda: state.update(pq=parquet_rows(), db=db_rows(state["cur"])),
             lambda: check_counts(state["cur"], state["pq"], state["db"]), lambda: check_chain(state["cur"]),
             lambda: check_point_in_time(state["cur"], state["db"]), lambda: check_starter(state["cur"], state["db"]),
             lambda: check_flags(state["cur"], state["db"]), lambda: check_live(state["db"], state["pq"]),
             lambda: check_hero_and_costs(state["cur"]), lambda: check_triage_recompute(state["db"]),
             lambda: check_resume(state["cur"])]
    for i, step in enumerate(steps):
        t = time.perf_counter()
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"F2.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
        print(f"  step {i}: {time.perf_counter() - t:.1f} s", flush=True)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
