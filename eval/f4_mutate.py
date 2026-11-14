"""F4.9: fault injection. Each F4 check that guards the run must fail on a single injected fault (and the F4.2 semantic
fix must not fire on legitimate semantic links).

    .venv/bin/python eval/f4_mutate.py              # character run, ~1.5 min: evidence/f4_mutations.json
    .venv/bin/python eval/f4_mutate.py --run sem    # semantic run (adds three semantic faults): evidence/f4s_mutations.json

Faults are injected into in-memory copies of the run, or into copies of the evidence files in a temporary directory;
logs/lineage.json is the one file changed in place, and it is restored from a byte copy.
"""
import argparse, copy, json, shutil, tempfile
from collections.abc import Callable
from pathlib import Path

import f4_check as F

def the_check(cid: str) -> dict:
    """The check with this id among those the call added (check_golden adds F4.3, F4.3b and F4.3c)."""
    return next(c for c in F.C.doc["checks"] if c["id"] == cid)


def first(events: dict, rule: str, kind: str | None = None) -> tuple[tuple[int, int], int]:
    for key in sorted(events):
        for j, e in enumerate(events[key]):
            if e[3] == rule and (kind is None or e[0] == kind):
                return key, j
    raise LookupError(rule)


def relabel(events: dict, rule: str, fn: Callable[[tuple], tuple], kind: str | None = None) -> dict:
    evs = copy.deepcopy(events); key, j = first(evs, rule, kind); evs[key][j] = fn(evs[key][j]); return evs


def run(results: list, name: str, check: str, expect: str, fn: Callable[[], None], needs: str = "") -> None:
    """needs: text that must appear in the check's result, so a fault is caught by the guard meant for it."""
    F.C.doc["checks"].clear(); fn(); c = the_check(check)
    status = c["status"]
    ok = status == expect and needs in c["got"]
    results.append(dict(mutation=name, check=check, expected=expect + (f" via '{needs}'" if needs else ""), status=status, ok=ok,
                        got=c["got"][:400]))
    print(f"{'ok ' if status == expect else 'BAD'} {check:6s} {name:60s} -> {status}")


def under_link(ids: dict, events: dict, rule: str = "ratio") -> tuple[dict, dict]:
    """A link (ratio edit or semantic) turned into delete + add of a fresh id (the claim's later revisions keep the old id)."""
    ids2, evs = copy.deepcopy(ids), copy.deepcopy(events)
    key, j = first(evs, rule); _, c, _, _ = evs[key][j]; fresh = max(max(v) for v in ids.values() if v) + 1
    ids2[key] = [fresh if x == c else x for x in ids2[key]]
    evs[key][j] = ("add", fresh, None, ""); evs[key].append(("delete", c, None, ""))
    return ids2, evs


def golden_with(texts: dict, mutate: Callable[[Path], None]) -> Callable[[], None]:
    def go() -> None:
        tmp = Path(tempfile.mkdtemp()); saved = F.EV
        try:
            for name in (f"{F.PRE}_pool.json", f"{F.PRE}_pool_blind.json", f"{F.PRE}_labels.json"):
                shutil.copy(saved / name, tmp / name)
            mutate(tmp); F.EV = tmp; F.check_golden(texts)
        finally:
            F.EV = saved; shutil.rmtree(tmp)
    return go


def edit_json(path: Path, fn: Callable[[object], None]) -> None:
    doc = json.loads(path.read_text(encoding="utf-8")); fn(doc); path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def move_pair(doc: dict) -> None:
    x = doc["pairs"][0]; x["stratum"] = next(s for s in doc["meta"]["populations"] if s != x["stratum"])


def wrong_log() -> None:
    """The run's log with another resolver's hash, from a temporary copy: the real log is never touched."""
    tmp = Path(tempfile.mkdtemp()); real = F.LOG
    try:
        doc = json.loads(real.read_text(encoding="utf-8")); doc["resolver_sha256"] = "0" * 64
        (tmp / real.name).write_text(json.dumps(doc), encoding="utf-8"); F.LOG = tmp / real.name
        F.check_replay()
    finally:
        F.LOG = real; shutil.rmtree(tmp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", choices=("char", "sem"), default="char")
    F.configure(ap.parse_args().run)
    out, pre = F.EV / f"{F.PRE}_mutations.json", F.PRE
    ids, events = F.load_ids(), F.load_events(); texts = F.claim_texts(F.D)
    rules = lambda evs: (lambda: F.check_rules(ids, evs))
    inv = lambda evs: (lambda: F.check_invariants(ids, evs))
    r: list[dict] = []
    run(r, "none (the stored run)", "F4.5b", "pass", rules(events))
    run(r, "a ratio edit's similarity off by 0.01", "F4.5b", "fail", rules(relabel(events, "ratio", lambda e: (e[0], e[1], e[2] + 0.01, e[3]))))
    run(r, "a near revival relabelled as a ratio edit", "F4.5b", "fail", rules(relabel(events, "near", lambda e: ("edit", e[1], e[2], "ratio"))))
    run(r, "a move relabelled as a revert", "F4.5b", "fail", rules(relabel(events, "move", lambda e: (e[0], e[1], e[2], "revert"))))
    run(r, "a containment edit relabelled as an exact revival", "F4.5b", "fail", rules(relabel(events, "contain", lambda e: ("revive", e[1], None, "exact"))))
    i2, e2 = under_link(ids, events)
    run(r, "under-linking: a ratio edit split into delete + add", "F4.5b", "fail", lambda: F.check_rules(i2, e2),
        needs="add beside a claim deleted in the same revision")
    if F.RUN == "sem":
        run(r, "a semantic link's cosine off by 0.01", "F4.5b", "fail",
            rules(relabel(events, "semantic", lambda e: (e[0], e[1], e[2] + 0.01, e[3]))))
        run(r, "a semantic link relabelled as a ratio edit", "F4.5b", "fail",
            rules(relabel(events, "semantic", lambda e: ("edit", e[1], e[2], "ratio"))))
        i3, e3 = under_link(ids, events, "semantic")
        run(r, "under-linking: a semantic link split into delete + add", "F4.5b", "fail", lambda: F.check_rules(i3, e3),
            needs="add beside an unused same-section claim")
    run(r, "none (the stored run)", "F4.2", "pass", inv(events))
    run(r, "a live ratio edit relabelled semantic (legitimate: keeps the id)", "F4.2", "pass",
        inv(relabel(events, "ratio", lambda e: ("semantic", e[1], 0.9, "semantic"))))
    run(r, "a near revival relabelled semantic (legitimate: brings the id back)", "F4.2", "pass",
        inv(relabel(events, "near", lambda e: ("semantic", e[1], 0.9, "semantic"))))
    key, _ = first(events, "", "delete"); gone = next(e[1] for e in events[key] if e[0] == "delete")
    bad = copy.deepcopy(events); bad[key] = bad[key] + [("semantic", gone, 0.9, "semantic")]
    run(r, "a semantic link to a claim deleted in that revision", "F4.2", "fail", inv(bad))
    run(r, "the run's log names another resolver", "F4.2b", "fail", wrong_log)
    run(r, "none (the stored golden set)", "F4.3", "pass", golden_with(texts, lambda d: None))
    run(r, "labels bound to another blind file", "F4.3", "fail",
        golden_with(texts, lambda d: edit_json(d / f"{pre}_labels.json", lambda x: x.update(blind_sha256="0" * 64))))
    run(r, "one labelled pair moved to another stratum", "F4.3", "fail",
        golden_with(texts, lambda d: edit_json(d / f"{pre}_pool.json", move_pair)))
    run(r, "one pair's text changed in the blind file", "F4.3", "fail",
        golden_with(texts, lambda d: edit_json(d / f"{pre}_pool_blind.json", lambda x: x[0].update(new=x[0]["new"] + " x"))))
    out.write_text(json.dumps(dict(run=F.RUN, **F.run_digests(), results=r), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sum(x['ok'] for x in r)} of {len(r)} as expected; wrote {out}")


if __name__ == "__main__":
    main()
