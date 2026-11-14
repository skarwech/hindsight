"""F6.7: fault injection. Each fault is a realistic loader bug, injected by wrapping pipeline/load_claims.page_rows while a
fresh copy loads into `smoke`; the F6 checks (round trip, set equality, integrity, the independent rebuild) then run on that
copy and must fail. A clean copy must pass.

    .venv/bin/python eval/f6_mutate.py      # ~1 min; writes docs/validation/evidence/f6_mutations.json
"""
import hashlib, json, sys
from collections import Counter
from collections.abc import Callable
from datetime import timedelta

import f6_lib as L
from common import ROOT, connect

sys.path.insert(0, str(ROOT / "pipeline"))
import load_claims  # noqa: E402

OUT = ROOT / "docs/validation/evidence/f6_mutations.json"
ORIGINAL = load_claims.page_rows


def parse(s: str):
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


def swap_text(p: int, rows: tuple) -> tuple:
    """A claim history row carries the text of the next row (an off-by-one in the loader)."""
    c_hist, c_cur, l_hist, l_cur = rows
    if len(c_hist) >= 2:
        a, b = c_hist[0], c_hist[1]
        c_hist = [a[:2] + b[2:4] + a[4:], *c_hist[1:]]
    return c_hist, c_cur, l_hist, l_cur


def late_close(p: int, rows: tuple) -> tuple:
    """A claim closed 1 µs after the revision that deleted it (row_end not a page_state time)."""
    c_hist, c_cur, l_hist, l_cur = rows
    r = c_hist[0]
    c_hist = [r[:-1] + (L.ts_text(parse(r[-1]) + timedelta(microseconds=1)),), *c_hist[1:]]
    return c_hist, c_cur, l_hist, l_cur


def drop_from_layout(p: int, rows: tuple) -> tuple:
    """One layout row loses its last claim id."""
    c_hist, c_cur, l_hist, l_cur = rows
    k = next(i for i, r in enumerate(l_hist) if json.loads(r[2]))
    r = l_hist[k]; order = json.loads(r[2])[:-1]
    l_hist = [*l_hist[:k], (r[0], r[1], json.dumps(order, separators=(",", ":")), r[3], r[4]), *l_hist[k + 1:]]
    return c_hist, c_cur, l_hist, l_cur


def overlap_row(p: int, rows: tuple) -> tuple:
    """A duplicate history row for a claim, opened one revision later than the original (overlapping periods)."""
    c_hist, c_cur, l_hist, l_cur = rows
    times = sorted({r[10] for r in c_hist} | {r[11] for r in c_hist})       # page_state times this page uses
    a = next(r for r in c_hist if any(r[10] < t < r[11] for t in times))     # a state spanning more than one revision
    inside = next(t for t in times if a[10] < t < a[11])
    return [*c_hist, a[:10] + (inside, L.ts_text(parse(a[11]) + timedelta(microseconds=7)))], c_cur, l_hist, l_cur


def blank_hidden(p: int, rows: tuple) -> tuple:
    """A hidden revision treated as a blank page: the layout empties at its time instead of carrying over."""
    c_hist, c_cur, l_hist, l_cur = rows
    hidden = HIDDEN.get(p)
    if not hidden:
        return rows
    t = hidden
    k = next(i for i, r in enumerate(l_hist) if parse(r[3]) < t < parse(r[4]))
    r = l_hist[k]
    l_hist = [*l_hist[:k], r[:4] + (L.ts_text(t),), (r[0], r[1], "[]", L.ts_text(t), r[4]), *l_hist[k + 1:]]
    return c_hist, c_cur, l_hist, l_cur


HIDDEN: dict[int, object] = {}


def load_with(fault: Callable | None, page: int | None) -> None:
    load_claims.page_rows = (lambda p, *a: fault(p, ORIGINAL(p, *a)) if p == page else ORIGINAL(p, *a)) if fault else ORIGINAL
    try:
        load_claims.load("smoke", fresh=True)
    finally:
        load_claims.page_rows = ORIGINAL


def evaluate(revs: dict, exp: dict, page: int | None) -> dict[str, bool]:
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
    rebuilt = Counter(L.rebuilt_claim_rows(revs)) == Counter(L.stored_claim_rows("smoke"))
    return {"F6.1": rt, "F6.2": sets, "F6.3": sum(integ.values()) == 0 and rebuilt}


def main() -> None:
    revs = L.revisions(); exp = L.expected_lists(revs)
    big = max(revs, key=lambda p: len(revs[p]))
    hid_page = next(p for p in sorted(revs) if any(h for _, _, h, _ in revs[p][1:]))
    HIDDEN[hid_page] = next(t for _, _, h, t in revs[hid_page][1:] if h)
    faults = [("none (a clean copy)", None, big, {"F6.1": True, "F6.2": True, "F6.3": True}),
              ("a claim row carries its neighbour's text", swap_text, big, {"F6.1": False, "F6.3": False}),
              ("a claim closed 1 µs late", late_close, big, {"F6.3": False}),
              ("the layout drops one claim id", drop_from_layout, big, {"F6.1": False, "F6.2": False}),
              ("a hidden revision blanks the page", blank_hidden, hid_page, {"F6.1": False}),
              ("an overlapping duplicate claim row", overlap_row, big, {"F6.3": False})]
    results = []
    for name, fault, page, expect in faults:
        load_with(fault, page)
        got = evaluate(revs, exp, page)
        for check, want in expect.items():
            results.append(dict(fault=name, page=page, check=check, expected="pass" if want else "fail",
                                status="pass" if got[check] else "fail", ok=got[check] == want))
            print(f"{'ok ' if got[check] == want else 'BAD'} {check} {name:45s} -> {'pass' if got[check] else 'fail'}")
    load_with(None, None)                                  # leave a clean copy behind
    sha = lambda f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
    OUT.write_text(json.dumps(dict(code={n: sha(n) for n in ("pipeline/load_claims.py", "eval/f6_lib.py", "eval/f6_mutate.py")},
                                   live_checksum=L.checksum("wikihistory"), results=results), indent=1, default=str), encoding="utf-8")
    print(f"{sum(r['ok'] for r in results)} of {len(results)} as expected; wrote {OUT}")


if __name__ == "__main__":
    main()
