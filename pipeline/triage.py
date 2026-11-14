"""Step 1 triage: rank all pages from page_state and freeze the hero set (docs/hero.tsv, docs/hero.md).

    .venv/bin/python pipeline/triage.py

Funnel (plan Step 1): 19.7k pages -> ~200 candidates -> the deep set. The deep set is the starter's 50
articles, every gold page of the starter's 37 test questions, and the top triage pages not yet in it.
A "dispute" is a named revert in the plan's sense (F10): a revert BY a named editor of a version saved
by a DIFFERENT named editor, neither of them a bot. That is stricter than the starter's code (which only
looks at the undone editor): the adversarial review found 41.5% of the starter-style disputes were
self-reverts, anonymous reverters or reverts across bot edits. A month with >= 3 disputes is an
edit-war month (the starter's WAR = 3). Plan deviation: ranked by edit-war months, not raw edit bursts
(max_day is reported but not scored), because bursts reward vandal waves.
"""
import csv, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from common import connect  # noqa: E402

MIN_VERSIONS = 100          # rates on tiny pages are noise
N_CANDIDATES = 200
N_TRIAGE_ADD = 10           # triage pages added to the deep set beyond the starter's and the gold pages
SVT = ROOT / "starters/mariadb-system-versioning-wikipedia/data/articles.tsv"
GOLD = ROOT / "starters/mariadb-vector-wikipedia-rag/data/test-questions.tsv"

# ponytail: bots by name suffix plus one known delinker; use Wikipedia's bot user group if precision matters
BOT = "(LOWER({e}) LIKE '%bot' OR {e} = 'CommonsDelinker')"
TRIAGE_SQL = f"""
WITH w AS (
  SELECT page_id, title, row_start, is_revert, anon, editor,
         LAG(anon) OVER (PARTITION BY page_id ORDER BY row_start) AS prev_anon,
         LAG(editor) OVER (PARTITION BY page_id ORDER BY row_start) AS prev_editor
  FROM page_state FOR SYSTEM_TIME ALL),
v AS (
  SELECT page_id, title, row_start, is_revert,
         COALESCE(is_revert AND NOT anon AND NOT prev_anon AND editor <> prev_editor
                  AND NOT {BOT.format(e='editor')} AND NOT {BOT.format(e='prev_editor')}, 0) AS dispute
  FROM w),
months AS (SELECT page_id, SUM(dispute) AS d FROM v GROUP BY page_id, DATE_FORMAT(row_start, '%Y-%m')),
days AS (SELECT page_id, COUNT(*) AS n FROM v GROUP BY page_id, DATE(row_start))
SELECT p.page_id, p.title, p.versions, p.reverts, p.disputes,
       COALESCE(w.war_months, 0) AS war_months, b.max_day
FROM (SELECT page_id, MAX(title) AS title, COUNT(*) AS versions, SUM(is_revert) AS reverts, SUM(dispute) AS disputes
      FROM v GROUP BY page_id) p
LEFT JOIN (SELECT page_id, COUNT(*) AS war_months FROM months WHERE d >= 3 GROUP BY page_id) w USING (page_id)
JOIN (SELECT page_id, MAX(n) AS max_day FROM days GROUP BY page_id) b USING (page_id)"""


def ranks(values: list[float]) -> list[float]:
    """Average rank, 1 = highest value."""
    order = sorted(range(len(values)), key=lambda i: -values[i])
    out = [0.0] * len(values); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def read_tsv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def main() -> None:
    cur = connect("wikihistory").cursor()
    t = time.perf_counter(); cur.execute(TRIAGE_SQL); rows = cur.fetchall(); ms = (time.perf_counter() - t) * 1000
    pages = [dict(page_id=r[0], title=r[1], versions=r[2], reverts=int(r[3]), disputes=int(r[4]),
                  war_months=int(r[5]), max_day=r[6], disputes_per_100=round(100 * int(r[4]) / r[2], 2)) for r in rows]
    big = [p for p in pages if p["versions"] >= MIN_VERSIONS]
    for p, a, b in zip(big, ranks([p["disputes_per_100"] for p in big]), ranks([p["war_months"] for p in big])):
        p["score_rank"] = (a + b) / 2          # rank-average of the two signals, lower = more contested
    big.sort(key=lambda p: (p["score_rank"], -p["versions"]))
    candidates = big[:N_CANDIDATES]
    by_title = {p["title"]: p for p in pages}
    deep: dict[int, str] = {}
    for a in read_tsv(SVT):
        deep[int(a["page_id"])] = f"starter: {a['why']}"
    gold = sorted({q["expected_fi_title"] for q in read_tsv(GOLD)})
    missing_gold = [g for g in gold if g not in by_title]
    for g in gold:
        if g in by_title:
            deep.setdefault(by_title[g]["page_id"], "gold page of a starter test question")
    added = [p for p in candidates if p["page_id"] not in deep][:N_TRIAGE_ADD]
    for i, p in enumerate(added, 1):
        deep[p["page_id"]] = f"triage #{big.index(p) + 1}: {p['disputes']} disputes, {p['war_months']} edit-war months"
    info = {p["page_id"]: p for p in pages}
    with open(ROOT / "docs/hero.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t"); w.writerow(["page_id", "title", "why", "versions", "disputes", "war_months"])
        for pid, why in sorted(deep.items(), key=lambda kv: info[kv[0]]["title"]):
            w.writerow([pid, info[pid]["title"], why, info[pid]["versions"], info[pid]["disputes"], info[pid]["war_months"]])
    md = [f"# Hero set (frozen {time.strftime('%Y-%m-%d')})", "",
          f"Generated by `pipeline/triage.py` from `page_state` ({len(pages):,} pages, triage query {ms:.0f} ms). "
          f"Machine-readable copy: `docs/hero.tsv`.", "",
          f"Deep set: {len(deep)} pages = the starter's 50 + gold pages of its 37 test questions not among them "
          f"+ the top {N_TRIAGE_ADD} triage pages not yet included. Missing gold titles: {missing_gold or 'none'}.", "",
          "| Page | Versions | Disputes | Edit-war months | Why |", "|---|---|---|---|---|"]
    md += [f"| {info[p]['title']} | {info[p]['versions']:,} | {info[p]['disputes']} | {info[p]['war_months']} | {why} |"
           for p, why in sorted(deep.items(), key=lambda kv: -info[kv[0]]["versions"])]
    md += ["", f"## Triage candidates (top {N_CANDIDATES} of {len(big):,} pages with >= {MIN_VERSIONS} versions)", "",
           "Rank-average of disputes per 100 versions and edit-war months (months with >= 3 disputes).", "",
           "| # | Page | Versions | Disputes /100 | Edit-war months | Max edits in a day | In deep set |", "|---|---|---|---|---|---|---|"]
    md += [f"| {i} | {p['title']} | {p['versions']:,} | {p['disputes_per_100']} | {p['war_months']} | {p['max_day']} | "
           f"{'yes' if p['page_id'] in deep else ''} |" for i, p in enumerate(candidates, 1)]
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "docs/hero.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (ROOT / "logs/triage.json").write_text(json.dumps(dict(ms=round(ms), pages=len(pages), big=len(big), deep=len(deep),
                                                           missing_gold=missing_gold, top=candidates[:25]), indent=1), encoding="utf-8")
    print(f"triage {ms:.0f} ms over {len(pages)} pages; deep set {len(deep)}; missing gold {missing_gold}")


if __name__ == "__main__":
    main()
