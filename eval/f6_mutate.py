"""F6.7: fault injection. Realistic loader and stored-text faults are applied to a fresh copy in `smoke`; the F6 checks
(round trip, set equality, SQL integrity, independent rebuild) then identify which sub-check catches each fault. A clean
copy must pass.

    .venv/bin/python eval/f6_mutate.py      # ~1 min; writes docs/validation/evidence/f6_mutations.json
"""
import hashlib, json, sys
from collections import Counter
from collections.abc import Callable
from datetime import timedelta

import duckdb
import f6_lib as L
from common import ROOT, connect

sys.path.insert(0, str(ROOT / "pipeline"))
import load_claims  # noqa: E402

OUT = ROOT / "docs/validation/evidence/f6_mutations.json"
ORIGINAL = load_claims.page_rows


def parse(s: str):
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


def swap_text(p: int, rows: tuple, *_args) -> tuple:
    """A claim history row carries the text of the next row (an off-by-one in the loader)."""
    c_hist, c_cur, l_hist, l_cur = rows
    if len(c_hist) >= 2:
        a, b = c_hist[0], c_hist[1]
        c_hist = [a[:2] + b[2:4] + a[4:], *c_hist[1:]]
    return c_hist, c_cur, l_hist, l_cur


def late_close(p: int, rows: tuple, page_revs: list, states: list, orders: dict, ts: dict, _meta: dict) -> tuple:
    """A claim closed 1 µs after the revision that deleted it (row_end not a page_state time)."""
    c_hist, c_cur, l_hist, l_cur = rows
    deleted = next(s for s in states if s[2] is not None and s[0] not in orders.get((p, s[2]), []))
    rev_at = {ordinal: rev for ordinal, rev, _hidden in page_revs}
    r = next(r for r in c_hist if r[1] == deleted[0] and r[5] == deleted[3])
    end = ts[(p, rev_at[deleted[2]])]
    c_hist = [row[:-1] + (L.ts_text(end + timedelta(microseconds=1)),) if row is r else row for row in c_hist]
    return c_hist, c_cur, l_hist, l_cur


def drop_from_layout(p: int, rows: tuple, *_args) -> tuple:
    """One layout row loses its last claim id."""
    c_hist, c_cur, l_hist, l_cur = rows
    k = next(i for i, r in enumerate(l_hist) if json.loads(r[2]))
    r = l_hist[k]; order = json.loads(r[2])[:-1]
    l_hist = [*l_hist[:k], (r[0], r[1], json.dumps(order, separators=(",", ":")), r[3], r[4]), *l_hist[k + 1:]]
    return c_hist, c_cur, l_hist, l_cur


def make_overlap_fixture(p: int) -> None:
    """Run the actual overlap SQL on plain copied periods; MariaDB rejects invalid overlap on the versioned table itself."""
    cur = connect("smoke").cursor()
    cur.execute("DROP TABLE IF EXISTS f6_overlap_fixture")
    cur.execute("CREATE TABLE f6_overlap_fixture (page_id INT, claim_id INT, row_start TIMESTAMP(6), row_end TIMESTAMP(6))")
    cur.execute("SELECT claim_id, row_start, row_end FROM claim FOR SYSTEM_TIME ALL WHERE page_id = ? "
                "AND row_end < '2106-01-01' ORDER BY claim_id, row_start", (p,))
    by_claim: dict[int, list[tuple]] = {}
    for claim_id, start, end in cur.fetchall():
        by_claim.setdefault(claim_id, []).append((start, end))
    cur.execute("SELECT row_start FROM wikihistory.page_state FOR SYSTEM_TIME ALL WHERE page_id = ? ORDER BY row_start", (p,))
    page_times = [row[0] for row in cur.fetchall()]
    for claim_id, periods in by_claim.items():
        for (start_a, end_a), (start_b, end_b) in zip(periods, periods[1:]):
            if end_a != start_b:
                continue
            later = next((t for t in page_times if t > end_b), None)
            if later is None:
                continue
            cur.executemany("INSERT INTO f6_overlap_fixture VALUES (?, ?, ?, ?)",
                            [(p, claim_id, start, end) for start, end in periods])
            cur.execute("UPDATE f6_overlap_fixture SET row_end = ? WHERE page_id = ? AND claim_id = ? AND row_start = ?",
                        (later, p, claim_id, start_a))
            return
    raise RuntimeError("no claim has two historical states followed by another page_state time")


def blank_hidden(p: int, rows: tuple, page_revs: list, _states: dict, _orders: dict, ts: dict, _meta: dict) -> tuple:
    """A hidden revision treated as a blank page: the layout empties at its time instead of carrying over."""
    c_hist, c_cur, l_hist, l_cur = rows
    hidden = HIDDEN.get(p)
    if not hidden:
        return rows
    t = hidden
    hidden_rev = next(rev for _, rev, is_hidden in page_revs if is_hidden and ts[(p, rev)] == t)
    k = next(i for i, r in enumerate(l_hist) if parse(r[3]) < t < parse(r[4]))
    r = l_hist[k]
    l_hist = [*l_hist[:k], r[:4] + (L.ts_text(t),), (r[0], hidden_rev, "[]", L.ts_text(t), r[4]), *l_hist[k + 1:]]
    return c_hist, c_cur, l_hist, l_cur


HIDDEN: dict[int, object] = {}


def every_revision_layout(p: int, rows: tuple, page_revs: list, _states: dict, orders: dict, ts: dict, _meta: dict) -> tuple:
    """Keep a layout row at every page_state revision, carrying order through hidden revisions."""
    c_hist, c_cur, _l_hist, _l_cur = rows
    order: list[int] = []
    layout_rows = []
    for ordinal, rev, hidden in page_revs:
        if not hidden:
            order = orders.get((p, ordinal), [])
        layout_rows.append((p, rev, json.dumps(order, separators=(",", ":")), load_claims.fmt(ts[(p, rev)])))
    l_hist = [row + (layout_rows[i + 1][3],) for i, row in enumerate(layout_rows[:-1])]
    return c_hist, c_cur, l_hist, layout_rows[-1:]


def page_with_repeated_order() -> int:
    """Pick a page whose consecutive visible revisions already have the same claim order."""
    d = duckdb.connect()
    orders = {(p, ordinal): order for p, ordinal, order in d.execute(
        f"""SELECT page_id, ord, list(claim_id ORDER BY pos)
             FROM '{ROOT}/data/lineage/claim_ids.parquet' GROUP BY page_id, ord""").fetchall()}
    previous: dict[int, list[int]] = {}
    for p, ordinal, hidden in d.execute(f"""SELECT page_id, ord, hidden FROM '{ROOT}/data/chunks/revisions.parquet'
                                           ORDER BY page_id, ord""").fetchall():
        if hidden:
            continue
        order = orders.get((p, ordinal), [])
        if p in previous and previous[p] == order:
            return p
        previous[p] = order
    raise RuntimeError("no page has consecutive revisions with an unchanged claim order")


def corrupt_variant_text() -> None:
    cur = connect("smoke").cursor()
    cur.execute("SELECT exact_hash FROM text_variant WHERE LENGTH(text) < 60000 LIMIT 1")
    exact_hash = cur.fetchone()[0]
    cur.execute("UPDATE text_variant SET text = CONCAT(text, ' fault') WHERE exact_hash = ?", (exact_hash,))


def load_with(fault: Callable | None, page: int | None, post_load: Callable | None = None) -> None:
    load_claims.page_rows = (lambda p, *a: fault(p, ORIGINAL(p, *a), *a) if p == page else ORIGINAL(p, *a)) if fault else ORIGINAL
    try:
        load_claims.load("smoke", fresh=True)
        if post_load:
            post_load()
    finally:
        load_claims.page_rows = ORIGINAL


def evaluate(revs: dict, exp: dict, page: int | None, overlap_fixture: bool = False) -> dict[str, bool]:
    """Each F6 check on the smoke copy, restricted to one page for the round trip (all pages when page is None)."""
    cur = connect("smoke").cursor(); rt = sets = True
    for p in [page] if page else list(revs):
        prev: list = []
        for o, r, hidden, t in revs[p]:
            for when, want in ((t - L.ONE_US, prev), (t, exp[(p, r)])):
                got, lay, alive = L.reconstruct(cur, "smoke", p, when)
                rt &= got == want; sets &= lay == alive
            prev = exp[(p, r)]
    integ = L.integrity("smoke")
    if overlap_fixture:
        fixture_cur = connect("smoke").cursor()
        integ["claim period overlap fixture"] = L.period_overlap_count(fixture_cur, "f6_overlap_fixture", "page_id, claim_id")
    rebuilt = Counter(L.rebuilt_claim_rows(revs)) == Counter(L.stored_claim_rows("smoke"))
    return {"F6.1": rt, "F6.2": sets, "F6.3a": sum(integ.values()) == 0, "F6.3b": rebuilt}


def main() -> None:
    revs = L.revisions(); exp = L.expected_lists(revs)
    big = max(revs, key=lambda p: len(revs[p]))
    repeated_order_page = page_with_repeated_order()
    hid_page = next(p for p in sorted(revs) if any(h for _, _, h, _ in revs[p][1:]))
    HIDDEN[hid_page] = next(t for _, _, h, t in revs[hid_page][1:] if h)
    faults = [("none (a clean copy)", None, big, None, []),
              ("a claim row carries its neighbour's text", swap_text, big, None, ["F6.1", "F6.3b"]),
              ("a claim closed 1 µs late", late_close, big, None, ["F6.2", "F6.3a", "F6.3b"]),
              ("the layout drops one claim id", drop_from_layout, big, None, ["F6.1", "F6.2"]),
              ("a hidden revision blanks the page", blank_hidden, hid_page, None, ["F6.1", "F6.2"]),
              ("an overlapping claim period with valid page_state endpoints", None, big, lambda: make_overlap_fixture(big), ["F6.3a"]),
              ("wrong text under a correct text_variant hash", None, None, corrupt_variant_text, ["F6.3a"]),
              ("a layout row for every revision", every_revision_layout, repeated_order_page, None, ["F6.3a"])]
    results = []
    for name, fault, page, post_load, expected_failed in faults:
        load_with(fault, page, post_load)
        got = evaluate(revs, exp, page, overlap_fixture=name.startswith("an overlapping claim period"))
        failed = sorted(check for check, passed in got.items() if not passed)
        ok = failed == expected_failed
        results.append(dict(fault=name, page=page, expected_failed=expected_failed, failed=failed, ok=ok))
        print(f"{'ok ' if ok else 'BAD'} {name}: expected {expected_failed}, got {failed}")
    load_with(None, None)                                  # leave a clean copy behind
    sha = lambda f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
    OUT.write_text(json.dumps(dict(code={n: sha(n) for n in L.MUTATION_CODE_FILES},
                                   live_checksum=L.checksum("wikihistory"), results=results), indent=1, default=str), encoding="utf-8")
    passed = sum(r["ok"] for r in results)
    print(f"{passed} of {len(results)} as expected; wrote {OUT}")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
