"""F4 — lineage resolver (plan section 7, F4; gate H12). Rebuilt after the adversarial review and code review.

    .venv/bin/python eval/f4_check.py              # character run (data/lineage): docs/validation/f4.json (~1 min)
    .venv/bin/python eval/f4_check.py --run sem    # semantic run (data/lineage_sem, Step 4): docs/validation/f4s.json

Needs data/lineage/ (pipeline/lineage.py), the golden set in docs/validation/evidence/ (eval/f4_pool.py, then two
judges: f4_labels.json bound to the blind pool's digest), Ollama for the real rewrite case, and MariaDB for page_state.
"""
import hashlib, json, math, re, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from itertools import groupby
from datetime import datetime, timezone

import argparse

import duckdb
import numpy as np
from rapidfuzz import fuzz, process

import f4_pool
from common import ROOT, Checks, connect, embed
from f4_pool import MISS_BANDS, SEM_BANDS, band, claim_texts, digest, strata

sys.path.insert(0, str(ROOT / "pipeline"))
from chunker import text_hash  # noqa: E402
from resolver import CONTAIN, LOOKBACK, MIN_CONTAIN, T_EDIT, T_NEAR, T_SEM, Claim, Memory, Para, resolve, similarity  # noqa: E402
import lineage  # noqa: E402

HARD = ("linked ratio 0.60–0.70", "linked ratio 0.70–0.80", "linked contain")   # and every "not linked" stratum (+ semantic links)
GAP: Counter = Counter()      # adds whose best match is a claim deleted in the last LOOKBACK revisions (F4.5b fills, F4.3c reports)
SWEEP: Counter = Counter()    # semantic run: links and near misses by cosine band (F4.5b fills, F4.3e reports)
CH, EV = ROOT / "data/chunks", ROOT / "docs/validation/evidence"
D = duckdb.connect()
Ids = dict[tuple[int, int], list[int]]
RUN, LIN, LOG, PRE, VEC = "char", ROOT / "data/lineage", ROOT / "logs/lineage.json", "f4", None
C = Checks("F4", "Lineage resolver", "H12")


def configure(run: str) -> None:
    """'char': the character run (data/lineage, evidence f4_*, docs/validation/f4.json); 'sem': the run with the semantic
    pass (data/lineage_sem, evidence f4s_*, f4s.json)."""
    global RUN, LIN, LOG, PRE, C, VEC
    RUN = run; sem = run == "sem"
    LIN, _, LOG = lineage.paths(sem)
    PRE = "f4s" if sem else "f4"
    C = (Checks("F4S", "Lineage with the semantic pass (Step 4 re-run of the F4 checks)", "H14") if sem
         else Checks("F4", "Lineage resolver", "H12"))
    f4_pool.LIN = LIN
    if sem:
        from embed_chunks import unit_vectors
        VEC = unit_vectors()
    GAP.clear(); SWEEP.clear()


def visible() -> list[tuple[int, int, int]]:
    return D.execute(f"SELECT page_id, ord, n_chunks FROM '{CH}/revisions.parquet' WHERE NOT hidden ORDER BY page_id, ord").fetchall()


def load_ids() -> Ids:
    out: Ids = defaultdict(list)
    for p, o, c in D.execute(f"SELECT page_id, ord, claim_id FROM '{LIN}/claim_ids.parquet' ORDER BY page_id, ord, pos").fetchall():
        out[(p, o)].append(c)
    return out


def load_events() -> dict[tuple[int, int], list[tuple[str, int, float | None, str]]]:
    out = defaultdict(list)
    for p, o, k, c, s, r in D.execute(f"SELECT page_id, ord, kind, claim_id, sim, rule FROM '{LIN}/events.parquet'").fetchall():
        out[(p, o)].append((k, c, s, r))
    return out


# ---------- F4.1 unit cases and the real rewrite case ----------
def check_units() -> None:
    run = lambda *a: subprocess.run([sys.executable, "-m", "pytest", "-q", *a, "test_resolver.py"], cwd=ROOT / "pipeline", capture_output=True, text=True)
    out, col = run(), run("--collect-only")
    m = re.search(r"(\d+) passed", out.stdout); passed = int(m[1]) if m else 0
    m = re.search(r"(\d+) tests? collected", col.stdout); collected = int(m[1]) if m else -1
    from test_resolver import P, REWRITE                    # the plan's sentences
    unit = lambda t: (lambda e: e / np.linalg.norm(e))(np.array(embed([t])[0]))
    vectors = {text_hash(p): unit(p) for p in (P[0], P[1], P[2], REWRITE)}
    cos = float(vectors[text_hash(P[1])] @ vectors[text_hash(REWRITE)])
    mem = Memory(); nid = iter(range(1, 100)).__next__
    a, _ = resolve([], [Para(text_hash(p), p, "") for p in P[:3]], mem, nid)
    prev = [Claim(i, text_hash(p), p, "") for i, p in zip(a, P[:3])]
    b, ev = resolve(prev, [Para(text_hash(p), p, "") for p in (P[0], REWRITE, P[2])], mem, nid, vectors=vectors)
    linked = b == a and [e.kind for e in ev] == ["semantic"]
    C.add("F4.1", "Unit cases: the plan's 9 (rewrite via the semantic pass with real bge-m3 vectors), simulation, and the review's cases",
          "invariant", "every case passes; the rewrite keeps its id via the semantic pass (cosine >= 0.88)",
          f"{passed} of {collected} passed (exit {out.returncode}); rewrite cosine {cos:.3f}, linked {linked}",
          out.returncode == 0 and passed == collected >= 23 and linked, out.stdout[-1500:])


# ---------- F4.2 invariants, replay ----------
def check_invariants(ids: Ids, events: dict) -> None:
    bad: Counter = Counter(); seen: dict[int, set[int]] = defaultdict(set); prev: dict[int, set[int]] = {}
    revs = visible()
    for p, o, n in revs:
        cur = ids.get((p, o), [])
        before, now = prev.get(p, set()), set(cur)
        ev = events.get((p, o), [])
        by = defaultdict(set)
        for k, c, _, _ in ev:
            by[k].add(c)
        bad["one id per chunk"] += len(cur) != n
        bad["unique in revision"] += len(now) != len(cur)
        bad["new ids != add|revive|semantic of a deleted claim"] += (now - before) != (by["add"] | by["revive"] | (by["semantic"] - before))
        bad["semantic link to an id not in the revision"] += len(by["semantic"] - now)
        bad["lost ids != delete"] += (before - now) != by["delete"]
        bad["edit of an absent id"] += len(by["edit"] - (before & now))
        bad["add of a seen id"] += len(by["add"] & seen[p])
        bad["revive of a live or unseen id"] += len(by["revive"] & before) + len(by["revive"] - seen[p])
        bad["two events for one id"] += len(ev) - len({c for _, c, _, _ in ev})
        seen[p] |= now; prev[p] = now
    C.add("F4.2", f"Invariants on all {len(revs):,} non-hidden revisions: events and id changes match both ways",
          "invariant", "0 violations of each kind", f"{sum(bad.values())} violations {dict(+bad) or ''}", sum(bad.values()) == 0)


def check_replay() -> None:
    log = json.loads(LOG.read_text(encoding="utf-8"))
    sha = hashlib.sha256((ROOT / "pipeline/resolver.py").read_bytes()).hexdigest()
    sem = RUN == "sem"
    want = dict(resolver_sha256=sha, t_edit=T_EDIT)
    if sem:
        from embed_chunks import store_digest
        want |= dict(t_sem=T_SEM, vectors_sha256=store_digest())
    same_code = all(log.get(k) == v for k, v in want.items())
    pages = [p for (p,) in D.execute(f"SELECT DISTINCT page_id FROM '{CH}/revisions.parquet' ORDER BY 1").fetchall()]
    tmp = tempfile.mkdtemp(); diff: dict[int, list[str]] = defaultdict(list); n = len(pages)
    try:
        with ProcessPoolExecutor(10) as pool:
            list(pool.map(lineage.lineage_page, pages, [tmp] * n, [T_EDIT] * n, [sem] * n, [T_SEM] * n))
        for part, name in (("ids", "claim_ids"), ("events", "events"), ("states", "states")):
            again, stored = f"read_parquet('{tmp}/{part}_*.parquet')", f"'{LIN}/{name}.parquet'"
            for p, n in D.execute(f"""SELECT page_id, COUNT(*) FROM ((SELECT * FROM {again} EXCEPT ALL SELECT * FROM {stored})
                                      UNION ALL (SELECT * FROM {stored} EXCEPT ALL SELECT * FROM {again})) GROUP BY 1""").fetchall():
                diff[p].append(f"{name}: {n} rows differ")
    finally:
        shutil.rmtree(tmp)
    named = "resolver.py, t_edit" + (", t_sem and the vector store's content" if sem else "")
    C.add("F4.2b", f"Replay: re-running the resolver on all {len(pages)} pages reproduces ids, events and states exactly; the stored run "
          f"was made by this {named}", "invariant", f"{len(pages)} of {len(pages)} pages identical; {named} match the run's log",
          f"{len(pages) - len(diff)} of {len(pages)} identical; " + ("; ".join(f"{k} {'matches' if log.get(k) == v else 'DIFFERS'}"
          for k, v in want.items())), not diff and same_code,
          "\n".join(f"page {p}: {'; '.join(d)}" for p, d in sorted(diff.items())) or "all identical")


# ---------- F4.4 restore oracle ----------
def check_restores(ids: Ids) -> None:
    cur = connect("wikihistory").cursor()
    pages = [p for (p,) in D.execute(f"SELECT DISTINCT page_id FROM '{CH}/revisions.parquet'").fetchall()]
    cur.execute(f"SELECT page_id, rev_id, sha1, undone FROM page_state FOR SYSTEM_TIME ALL WHERE page_id IN ({','.join(map(str, pages))}) "
                "ORDER BY page_id, row_start, rev_id")
    by_page = defaultdict(list)
    for p, r, s, u in cur.fetchall():
        by_page[p].append((r, s, u))
    ord_of = {(p, r): o for p, r, o in D.execute(f"SELECT page_id, rev_id, ord FROM '{CH}/revisions.parquet'").fetchall()}
    episodes = []
    for p, vs in by_page.items():
        last: dict[str, int] = {}
        for i, (r, s, u) in enumerate(vs):
            if s and i and vs[i - 1][1] != s and s in last and i - last[s] >= 2:
                a, b = (p, ord_of[(p, vs[last[s]][0])]), (p, ord_of[(p, r)])
                if ids.get(a) or ids.get(b):                       # skip restores of an empty page: nothing to compare
                    episodes.append((p, vs[last[s]][0], r, all(x[2] for x in vs[last[s] + 1:i]), ids.get(a) == ids.get(b)))
            if s:
                last[s] = i
    vandal = [e[4] for e in episodes if e[3]]
    allr = [e[4] for e in episodes]
    C.add("F4.4", "Vandalism oracle: after each vandalism episode (undone-flagged revisions, then an exact restore) every claim id returns",
          "oracle", "plan: 20 of 20; here every episode in the hero pages", f"{sum(vandal):,} of {len(vandal):,} identical",
          all(vandal) and len(vandal) >= 20)
    C.add("F4.4b", "Every exact restore, vandalism or not: claim ids equal those of the restored revision", "oracle",
          "recorded; tests restore bookkeeping (the graveyard), not whether ids follow the right paragraph (that is F4.3)",
          f"{sum(allr):,} of {len(allr):,} identical ({sum(allr) / len(allr):.2%})", None,
          "\n".join(f"page {p}: rev {a} -> {b}" for p, a, b, _, ok in episodes if not ok) or "all identical")


# ---------- F4.5 accounting ----------
def check_accounting(ids: Ids, events: dict) -> None:
    hashes = defaultdict(list)
    for p, o, h in D.execute(f"SELECT page_id, ord, text_hash FROM '{CH}/rev_chunks.parquet' ORDER BY page_id, ord, pos").fetchall():
        hashes[(p, o)].append(h)
    bad: Counter = Counter(); prev_text: dict[int, dict[int, int]] = {}
    for p, o, _ in visible():
        now = dict(zip(ids.get((p, o), []), hashes.get((p, o), [])))
        before = prev_text.get(p, {})
        ev = defaultdict(list)
        for k, c, _, r in events.get((p, o), []):
            if k != "delete":
                ev[c].append((k, r))
        for c, h in now.items():
            e = ev.get(c, [])
            if c not in before:
                bad["new to the revision without exactly one add/revive/semantic"] += not (len(e) == 1 and e[0][0] in ("add", "revive", "semantic"))
            elif before[c] != h:
                bad["changed text without exactly one edit/semantic"] += not (len(e) == 1 and e[0][0] in ("edit", "semantic"))
            else:
                bad["unchanged text with an event other than a move"] += not (not e or e == [("edit", "move")])
        bad["event for an id not in the revision"] += len(set(ev) - set(now))
        prev_text[p] = now
    C.add("F4.5", "Accounting: each claim new to a revision has one add/revive/semantic event, each changed text one edit, nothing else",
          "invariant", "0 violations", f"{sum(bad.values())} violations {dict(+bad) or ''}", sum(bad.values()) == 0)


def check_rules(ids: Ids, events: dict) -> None:
    """Each event satisfies the condition of the rule that decided it, recomputed from the chunk texts (aligned with the
    given ids by position, so the check judges these ids, not the stored ones)."""
    hashes, paras, secs = defaultdict(list), defaultdict(list), defaultdict(list)
    for p, o, h, t, sec in D.execute(f"""SELECT r.page_id, r.ord, r.text_hash, v.text, r.section FROM '{CH}/rev_chunks.parquet' r
                                    JOIN '{CH}/chunk_variant.parquet' v USING (exact_hash) ORDER BY r.page_id, r.ord, r.pos""").fetchall():
        hashes[(p, o)].append(h); paras[(p, o)].append((t,)); secs[(p, o)].append(sec)
    bad: Counter = Counter(); seen: Counter = Counter()
    prev_h: dict[int, dict[int, int]] = defaultdict(dict); prev_t: dict[int, dict] = defaultdict(dict)
    prev_s: dict[int, dict[int, str]] = defaultdict(dict)
    cosine = lambda a, b: float(VEC[a] @ VEC[b]) if VEC is not None and a in VEC and b in VEC else -1.0
    hist: dict[tuple[int, int], set[int]] = {}             # (page, claim) -> every text hash it had in earlier revisions
    died: dict[int, dict[int, tuple]] = defaultdict(dict)  # page -> dead claim -> (visible index of deletion, last text, hash, section)
    k: Counter = Counter()                                 # visible index per page (the resolver's sequence number - 1)
    close = lambda a, b: abs(a - b) < 1e-4                 # sims are stored as float32
    for p, o, _ in visible():
        now_h = dict(zip(ids.get((p, o), []), hashes.get((p, o), []), strict=True))
        now_t = dict(zip(ids.get((p, o), []), paras.get((p, o), []), strict=True))
        now_s = dict(zip(ids.get((p, o), []), secs.get((p, o), []), strict=True))
        bh, bt, bs = prev_h[p], prev_t[p], prev_s[p]
        for kind, c, sim, rule in events.get((p, o), []):
            key = f"{kind} {rule}".strip(); seen[key] += 1
            new, had = now_t.get(c, ("",))[0], hist.get((p, c), set())
            if rule == "move":
                ok = c in bh and bh[c] == now_h.get(c)
            elif rule == "revert":
                ok = c in bh and c in now_h and now_h[c] != bh[c] and now_h[c] in had
            elif rule == "exact":
                ok = c not in bh and c in now_h and now_h[c] in had
            elif rule == "near":
                at, last, _, _ = died[p].get(c, (-99, "", None, None))
                ok = c not in bh and k[p] - at <= LOOKBACK and sim >= T_NEAR - 1e-6 and close(fuzz.ratio(last, new) / 100, sim)
            elif rule == "ratio":
                ok = c in bh and sim >= T_EDIT - 1e-6 and close(fuzz.ratio(bt[c][0], new) / 100, sim)
            elif rule == "contain":
                old = bt.get(c, ("",))[0]; part = fuzz.partial_ratio(old, new) / 100
                ok = (len(old) >= MIN_CONTAIN and len(new) >= 2 * len(old) and part >= CONTAIN and sim >= T_EDIT - 1e-6
                      and close(0.9 * part + 0.05 * fuzz.ratio(old, new) / 100, sim))
            elif kind == "add":
                ok = (p, c) not in hist and c in now_h
            elif kind == "delete":
                ok = c in bh and c not in now_h
            elif kind == "semantic":                       # a live claim rewritten, or one deleted in the previous revision
                if c in bh:
                    old_h, old_s, fresh = bh[c], bs[c], now_h.get(c) != bh[c]
                else:
                    at, _, old_h, old_s = died[p].get(c, (-99, "", None, None)); fresh = at == k[p] - 1
                cs = cosine(old_h, now_h.get(c))
                ok = fresh and old_s == now_s.get(c) and sim >= T_SEM - 1e-6 and close(cs, sim)
                SWEEP[f"linked {band(cs, SEM_BANDS)}"] += 1
            else:
                ok = False
            bad[key] += not ok
        adds = [c for kind, c, _, _ in events.get((p, o), []) if kind == "add"]
        if adds:                                           # maximality: no add beside an unused candidate the rules would link
            new_txt = [now_t[c][0] for c in adds]
            gone_now = list(bh.keys() - now_h.keys())
            if gone_now:
                score, _ = similarity([bt[c][0] for c in gone_now], new_txt)
                bad["add beside a claim deleted in the same revision at >= t_edit"] += int((score >= T_EDIT - 1e-6).any(axis=0).sum())
            dead = [(c, t) for c, (at, t, _, _) in died[p].items() if c not in now_h and k[p] - at <= LOOKBACK]
            if dead:
                best = (process.cdist([t for _, t in dead], new_txt, scorer=fuzz.ratio) / 100).max(axis=0)
                bad[f"add beside a recently deleted claim at >= {T_NEAR}"] += int((best >= T_NEAR - 1e-6).sum())
                GAP.update("0.75–0.90" if b >= 0.75 else "0.60–0.75" for b in best if T_EDIT <= b < T_NEAR)
            if VEC is not None:                            # semantic maximality, and the near misses for the sweep
                olds = [(bh[c], bs[c]) for c in gone_now] + [(h, sc) for c, (at, _, h, sc) in died[p].items()
                                                             if at == k[p] - 1 and c not in now_h]
                for x in adds:
                    best = max((cosine(h, now_h[x]) for h, sc in olds if sc == now_s[x]), default=-1.0)
                    bad[f"add beside an unused same-section claim at cosine >= {T_SEM}"] += best >= T_SEM - 1e-6
                    if band(best, MISS_BANDS):
                        SWEEP[f"near miss {band(best, MISS_BANDS)}"] += 1
        for c in now_h.keys() - bh.keys():
            died[p].pop(c, None)
        for c in bh.keys() - now_h.keys():
            died[p][c] = (k[p], bt[c][0], bh[c], bs[c])
        for c, h in now_h.items():
            hist.setdefault((p, c), set()).add(h)
        prev_h[p], prev_t[p], prev_s[p] = now_h, now_t, now_s; k[p] += 1
    C.add("F4.5b", "Rule consistency: every event meets the condition of the rule that decided it, recomputed from the texts",
          "invariant", "0 violations: move = same text; revert / exact revival = a text the claim had before; near revival = ratio >= "
          f"{T_NEAR} to its last text, deleted <= {LOOKBACK} revisions earlier; ratio edit = ratio >= {T_EDIT}; containment = grown >= 2x, "
          f"old >= {MIN_CONTAIN} chars, partial >= {CONTAIN}; sims recomputed exactly; and no add is left beside an unused deleted claim "
          f"the rules would link (>= {T_EDIT} same revision, >= {T_NEAR} within {LOOKBACK} revisions"
          + (f", same section at cosine >= {T_SEM} in this or the previous revision; semantic = same section, cosine recomputed "
             f">= {T_SEM}, the old claim live or deleted one revision earlier)" if VEC is not None else ")"),
          f"{sum(bad.values())} violations in {sum(seen.values()):,} events {dict(+bad) or ''}", sum(bad.values()) == 0,
          json.dumps(dict(seen.most_common())))


# ---------- F4.7 states ----------
def check_states(ids: Ids) -> None:
    rows = D.execute(f"""SELECT i.page_id, i.ord, i.claim_id, r.text_hash, r.exact_hash, r.n_refs, r.section, i.rev_id
                         FROM '{LIN}/claim_ids.parquet' i JOIN '{CH}/rev_chunks.parquet' r USING (page_id, rev_id, ord, pos)
                         ORDER BY i.page_id, i.claim_id, i.ord""").fetchall()
    vis = defaultdict(list)
    for p, o, _ in visible():
        vis[p].append(o)
    nxt = {(p, a): b for p, os in vis.items() for a, b in zip(os, os[1:] + [None])}
    rebuilt = []
    for (p, c), grp in groupby(rows, key=lambda r: (r[0], r[2])):
        runs: list[dict] = []                              # a state lasts while the claim is in the next visible revision, unchanged
        for _, o, _, th, eh, nr, sec, rev in grp:
            key = (th, eh, nr, sec)
            if runs and runs[-1]["until"] == o and runs[-1]["key"] == key:
                runs[-1]["until"] = nxt[(p, o)]
            else:
                runs.append(dict(start=o, rev=rev, key=key, until=nxt[(p, o)]))
        rebuilt += [(p, c, r["start"], r["until"], r["rev"]) + r["key"] for r in runs]
    stored = D.execute(f"SELECT page_id, claim_id, start_ord, end_ord, start_rev, text_hash, exact_hash, n_refs, section FROM '{LIN}/states.parquet'").fetchall()
    same = Counter(rebuilt) == Counter(stored)
    overl = D.execute(f"""SELECT COUNT(*) FROM (SELECT start_ord, end_ord, LEAD(start_ord) OVER (PARTITION BY page_id, claim_id ORDER BY start_ord) nx
                          FROM '{LIN}/states.parquet') WHERE (end_ord IS NOT NULL AND end_ord <= start_ord) OR (nx IS NOT NULL AND (end_ord IS NULL OR nx < end_ord))""").fetchall()[0][0]
    # reconstruct every visible revision from the states, in claim_ids order
    st = defaultdict(list)
    for p, c, s, e, eh in D.execute(f"SELECT page_id, claim_id, start_ord, end_ord, exact_hash FROM '{LIN}/states.parquet'").fetchall():
        st[(p, c)].append((s, e, eh))
    want = defaultdict(list)
    for p, o, eh in D.execute(f"SELECT page_id, ord, exact_hash FROM '{CH}/rev_chunks.parquet' ORDER BY page_id, ord, pos").fetchall():
        want[(p, o)].append(eh)
    wrong = 0
    for p, o, _ in visible():
        got = [next((eh for s, e, eh in st[(p, c)] if s <= o and (e is None or o < e)), None) for c in ids.get((p, o), [])]
        wrong += got != want.get((p, o), [])
    C.add("F4.7", "Claim states (Step 5's history rows): rebuilt independently from ids + chunks, no overlaps, every revision reconstructs",
          "invariant", "states identical to the rebuild; 0 overlaps; every visible revision's exact text list reconstructs",
          f"rebuild identical: {same} ({len(stored):,} states); {overl} overlaps; {wrong} of {len(visible()):,} revisions fail to reconstruct",
          same and overl == 0 and wrong == 0)


# ---------- F4.3 golden set ----------
def wilson_lower(p: float, n: float, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def check_golden(texts: dict) -> None:
    pool_doc = json.loads((EV / f"{PRE}_pool.json").read_text(encoding="utf-8"))
    meta, pool = pool_doc["meta"], {x["id"]: x for x in pool_doc["pairs"]}
    blind_path, lab_path = EV / f"{PRE}_pool_blind.json", EV / f"{PRE}_labels.json"
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    lab = json.loads(lab_path.read_text(encoding="utf-8")) if lab_path.exists() else {}
    bound = isinstance(lab, dict) and lab.get("blind_sha256") == digest(blind_path)
    matches = len(blind) == len(pool) and all(pool.get(x["id"], {}).get(k) == x[k] for x in blind for k in ("page", "section", "old", "new"))
    now = strata(D, texts, VEC)                            # every judged decision of the current run, by stratum
    members = {k: {(p, o, a, b) for p, o, a, b, _, _ in v} for k, v in now.items()}
    moved = [i for i, x in pool.items() if (x["page_id"], x["ord"], x["old"], x["new"]) not in members.get(x["stratum"], set())]
    pops = {k: len(v) for k, v in now.items()}
    n_in = Counter(x["stratum"] for x in pool.values())
    uncovered = [k for k in pops if k not in n_in]
    judges = lab.get("judges", []) if isinstance(lab, dict) else []
    votes = defaultdict(dict)
    for j in judges:
        for l in j["labels"] or []:
            votes[l["id"]][j["judge"]] = l["same"]
    complete = len(judges) >= 2 and all(len(votes[i]) == len(judges) for i in pool)
    fresh = (bound and matches and not moved and not uncovered and meta["t_edit"] == T_EDIT
             and meta.get("t_sem", None) == (T_SEM if RUN == "sem" else None))
    if not (fresh and complete):
        C.add("F4.3", "Golden set", "golden", "labels bound to the blind pool; every pair still in its stratum in this run; 2 judges",
              f"labels bound {bound}; blind = pool {matches}; pairs moved {moved[:10]}; strata not sampled {uncovered}; complete {complete}", False)
        return
    w = {i: pops[x["stratum"]] / n_in[x["stratum"]] for i, x in pool.items()}   # weights from the current run
    linked = {i for i, x in pool.items() if x["stratum"].startswith("linked")}
    agree = {i: v[next(iter(v))] for i, v in votes.items() if len(set(v.values())) == 1}

    def metrics(subset: list[int], worst: bool) -> tuple[float, float, float, float]:
        # worst case: a disagreement counts against the resolver (a link judged different, a non-link judged same)
        y = {i: agree.get(i, i not in linked) if worst else agree[i] for i in subset if worst or i in agree}
        tp = sum(w[i] for i in y if i in linked and y[i]); fp = sum(w[i] for i in y if i in linked and not y[i])
        fn = sum(w[i] for i in y if i not in linked and y[i])
        n_eff = lambda ids: sum(w[i] for i in ids) ** 2 / max(sum(w[i] ** 2 for i in ids), 1e-9)
        prec, rec = tp / max(tp + fp, 1e-9), tp / max(tp + fn, 1e-9)
        return prec, rec, wilson_lower(prec, n_eff([i for i in y if i in linked])), wilson_lower(rec, n_eff([i for i in y if y[i]]))
    every = list(pool)
    hard = [i for i in pool if pool[i]["stratum"] in HARD or pool[i]["stratum"].startswith("linked semantic") or i not in linked]
    p_a, r_a, _, _ = metrics(every, False)
    p_w, r_w, plb_w, rlb_w = metrics(every, True)
    hp_w, hr_w, hplb_w, hrlb_w = metrics(hard, True)
    hp_a, hr_a, hplb_a, hrlb_a = metrics(hard, False)
    rows = []
    for s in sorted(n_in):
        ids = [i for i in pool if pool[i]["stratum"] == s]
        rows.append([s, pops[s], meta["populations"].get(s, 0), len(ids), sum(1 for i in ids if agree.get(i) is True),
                     sum(1 for i in ids if agree.get(i) is False), sum(1 for i in ids if i not in agree)])
    C.table("Golden set by stratum: population in this run (and when drawn), sampled, judged same / different / judges disagree",
            ["Stratum", "Population", "At draw", "Sampled", "Same", "Different", "Disagree"], rows)
    wrong = [f"#{i} {pool[i]['stratum']} score {pool[i]['score']}: judged {'same' if y else 'different'} | OLD {pool[i]['old'][:120]!r} | "
             f"NEW {pool[i]['new'][:120]!r}" for i, y in sorted(agree.items()) if y != (i in linked)]
    C.add("F4.3", f"Golden set: precision and recall of the similarity-based decisions, {len(pool)} real transitions, weighted by this run",
          "golden", "precision >= 95%, recall >= 80% (plan), worst case (disagreements count against); two model judges, not humans",
          f"worst case: precision {p_w:.1%} (95% lower bound {plb_w:.1%}), recall {r_w:.1%} (lower bound {rlb_w:.1%}); agreed labels only: "
          f"precision {p_a:.1%}, recall {r_a:.1%}; judges agree on {len(agree)} of {len(pool)}; labels bound to the blind pool "
          f"({lab['blind_sha256'][:12]}…), all {len(pool)} pairs still decided as drawn", p_w >= 0.95 and r_w >= 0.80,
          "Resolver disagrees with the agreed label:\n" + "\n".join(wrong))
    C.add("F4.3b", f"Hard decisions only: ratio links below 0.80, containment links and every non-link ({len(hard)} pairs)", "golden",
          "recorded, not gated: the plan's recall is dominated by 17k near-identical typo fixes; this is the number that can move",
          f"worst case: precision {hp_w:.1%} (lower bound {hplb_w:.1%}), recall {hr_w:.1%} (lower bound {hrlb_w:.1%}); agreed only: "
          f"precision {hp_a:.1%} (lower bound {hplb_a:.1%}), recall {hr_a:.1%} (lower bound {hrlb_a:.1%})", None)
    if RUN == "char":
        C.add("F4.3c", "Not measured: non-links across revisions (an added paragraph whose best match is a claim deleted up to "
              f"{LOOKBACK} revisions earlier, below the {T_NEAR} near-revival threshold)", "golden",
              "recorded: the pool's non-link strata compare a revision's deletions with its own additions only; Step 4's golden set covers these",
              f"{sum(GAP.values()):,} added paragraphs: {GAP['0.75–0.90']:,} at 0.75–0.90, {GAP['0.60–0.75']:,} at 0.60–0.75; not judged", None)
        return
    sem_ids = [i for i in pool if pool[i]["stratum"].startswith("linked semantic")]
    sp_w, _, splb_w, _ = metrics(sem_ids, True)
    sp_a, _, splb_a, _ = metrics(sem_ids, False)
    C.add("F4.3s", f"Semantic links: precision at the plan's fixed threshold (cosine >= {T_SEM}, same section), {len(sem_ids)} judged pairs",
          "golden", "precision >= 95% worst case (threshold fixed in advance); below that, Step 5 keeps the character-only lineage",
          f"worst case: precision {sp_w:.1%} (95% lower bound {splb_w:.1%}); agreed only: {sp_a:.1%} (lower bound {splb_a:.1%})",
          sp_w >= 0.95)
    cross = [i for i in pool if pool[i]["stratum"].startswith("not linked, across")]
    C.add("F4.3c", "Non-links across revisions (an addition whose best match is a claim deleted up to "
          f"{LOOKBACK} revisions earlier at ratio 0.60–0.90), judged in this pool", "golden",
          "recorded: the share a reader would call the same paragraph (each one is a missed link)",
          f"{sum(GAP.values()):,} additions; judged same by both in {sum(agree.get(i) is True for i in cross)} of {len(cross)}, "
          f"different in {sum(agree.get(i) is False for i in cross)}, disputed {sum(i not in agree for i in cross)}", None)
    rows = []
    for name in sorted(n_in):
        if name.startswith(("linked semantic", "not linked, semantic near miss")):
            ids = [i for i in pool if pool[i]["stratum"] == name]
            rows.append([name, pops[name], len(ids), sum(agree.get(i) is True for i in ids), sum(agree.get(i) is False for i in ids),
                         sum(i not in agree for i in ids)])
    C.table("Threshold sweep (report only): semantic candidates by cosine band; below 0.88 they are not linked, so 'same' there is a "
            "missed link, above it 'different' is a wrong one", ["Band", "Population", "Sampled", "Judged same", "Judged different",
                                                                  "Disagree"], rows)
    C.add("F4.3e", f"Threshold sweep around t_sem = {T_SEM} (report only; a different threshold would need a fresh sample)", "golden",
          "recorded", "; ".join(f"{r[0].replace('not linked, semantic ', '')}: {r[3]}/{r[2]} same" for r in rows), None)


def check_merge_changes() -> None:
    """The decisions the last resolver change (one assignment for rewrites and near-duplicate revivals) altered, judged blind."""
    cases = json.loads((EV / "f4_merge_changed.json").read_text(encoding="utf-8"))
    key = {x["id"]: x for x in json.loads((EV / "f4_merge_key.json").read_text(encoding="utf-8"))}
    lab = json.loads((EV / "f4_merge_labels.json").read_text(encoding="utf-8"))
    bound = lab["blind_sha256"] == digest(EV / "f4_merge_blind.json")
    votes = defaultdict(list)
    for j in lab["judges"]:
        for l in j["labels"]:
            votes[(key[l["id"]]["case"], key[l["id"]]["role"])].append(l["same"])
    word = lambda v: "same" if all(v) else "different" if not any(v) else "disputed"
    rows = [[c["page"], c["ord"], c["merged"]["score"], word(votes[(k, "dead")]), c["pre_merge"]["rule"], c["pre_merge"]["score"],
             word(votes[(k, "live")])] for k, c in enumerate(cases)]
    C.table("Decisions changed by the last resolver change: a near-duplicate of a just-deleted claim now beats a weaker live match "
            "(both judges, blind)", ["Page", "Revision", "Merged: near revival", "Judged", "Pre-merge link", "Score", "Judged"], rows)
    n = len(cases); good = sum(r[3] == "same" for r in rows)
    C.add("F4.3d", f"The {n} decisions the merged assignment changed, judged blind (2 pairs each: the revived claim and the live claim "
          "the old code chose)", "golden", "recorded: the merged choice should be judged the same paragraph",
          f"merged choice judged same by both judges in {good} of {n}; the old choice: {sum(r[6] == 'different' for r in rows)} different, "
          f"{sum(r[6] == 'disputed' for r in rows)} disputed, {sum(r[6] == 'same' for r in rows)} same (duplicates: either id is defensible); "
          f"labels bound to the blind file: {bound}", None if bound else False)


def check_mutations() -> None:
    path = EV / f"{PRE}_mutations.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"results": []}
    fresh = (doc.get("resolver_sha256") == hashlib.sha256((ROOT / "pipeline/resolver.py").read_bytes()).hexdigest()
             and doc.get("f4_check_sha256") == hashlib.sha256(open(__file__, "rb").read()).hexdigest())
    r = doc["results"]
    C.table("Fault injection (eval/f4_mutate.py): each guard fails on its fault, and legitimate cases pass",
            ["Check", "Injected fault", "Expected", "Got"], [[x["check"], x["mutation"], x["expected"], x["status"]] for x in r])
    C.add("F4.9", "Fault injection: F4.2, F4.2b, F4.3 and F4.5b each fail on a single injected fault and pass on the stored run",
          "invariant", "every outcome as expected, recorded for this resolver.py and this f4_check.py",
          f"{sum(x['ok'] for x in r)} of {len(r)} as expected; fresh {fresh}", bool(r) and all(x["ok"] for x in r) and fresh)


# ---------- F4.6 sizes, F4.8 biography ----------
def check_sizes(ids: Ids) -> None:
    log = json.loads(LOG.read_text(encoding="utf-8"))
    titles = dict(D.execute(f"SELECT page_id::INT, title FROM read_csv('{ROOT}/docs/hero.tsv', delim='\t', header=true)").fetchall())
    revs = dict(D.execute(f"SELECT page_id, COUNT(*) FROM '{CH}/revisions.parquet' GROUP BY 1").fetchall())
    per = sorted(log["per_page"], key=lambda s: -revs[s["page_id"]])
    worst = max(per, key=lambda s: s["claims"] / max(s["current"], 1))
    lived = sum(1 for _ in D.execute(f"SELECT 1 FROM '{LIN}/claim_ids.parquet' GROUP BY page_id, claim_id HAVING COUNT(*) > 1").fetchall())
    rules = D.execute(f"SELECT kind || COALESCE(' ' || NULLIF(rule, ''), ''), COUNT(*) FROM '{LIN}/events.parquet' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    ev_per = D.execute(f"""SELECT quantile_cont(n, 0.5), quantile_cont(n, 0.9) FROM (SELECT r.page_id, r.ord, COUNT(e.kind) n
                           FROM '{CH}/revisions.parquet' r LEFT JOIN '{LIN}/events.parquet' e USING (page_id, ord) WHERE NOT r.hidden GROUP BY 1, 2)""").fetchall()[0]
    C.add("F4.6", "Sizes and speed: claims ever vs alive, events per revision, history rows, resolver time", "smoke",
          "0–2 events per ordinary revision; claims ever <= 20x alive (warning sign); est. ~1 min for a 3,357-version page",
          f"{log['claims']:,} claims ever, {lived:,} lived more than one revision ({lived / log['current']:.1f}x the {log['current']:,} alive at "
          f"dataset end); worst page {titles.get(worst['page_id'])} {worst['claims'] / max(worst['current'], 1):.1f}x; events per revision median "
          f"{ev_per[0]:g}, P90 {ev_per[1]:g}; {log['states']:,} states ({log['states'] / sum(revs.values()):.2f} per revision); "
          f"all 60 pages in {log['seconds']} s, slowest page {max(s['seconds'] for s in per)} s", None, json.dumps(rules))
    C.table("Events by kind and deciding rule", ["Event", "Count"], [[k, n] for k, n in rules])
    if RUN == "sem":
        base = json.loads(lineage.paths(False)[2].read_text(encoding="utf-8"))
        keys = ("claims", "current", "states", "add", "delete", "edit", "revive", "semantic")
        C.table("Character run (Step 3) vs semantic run (Step 4)", ["Measure", "Character run", "Semantic run", "Change"],
                [[k, base.get(k, 0), log.get(k, 0), log.get(k, 0) - base.get(k, 0)] for k in keys])
    C.table("Largest hero pages: claims ever seen, alive at dataset end, claim states",
            ["Page", "Revisions", "Claims ever", "Alive", "Ratio", "States"],
            [[titles.get(s["page_id"], str(s["page_id"])), revs[s["page_id"]], s["claims"], s["current"],
              round(s["claims"] / max(s["current"], 1), 1), s["states"]] for s in per[:12]])


def check_biography() -> None:
    cid = D.execute(f"SELECT claim_id FROM '{LIN}/states.parquet' WHERE page_id = 1256 GROUP BY 1 ORDER BY COUNT(DISTINCT text_hash) DESC LIMIT 1").fetchall()[0][0]
    st = D.execute(f"""SELECT s.start_rev, s.n_refs, v.text FROM '{LIN}/states.parquet' s JOIN '{CH}/chunk_variant.parquet' v USING (exact_hash)
                       WHERE s.page_id = 1256 AND s.claim_id = ? ORDER BY s.start_ord""", [cid]).fetchall()
    cur = connect("wikihistory").cursor()
    cur.execute(f"SELECT rev_id, DATE(row_start), editor, anon FROM page_state FOR SYSTEM_TIME ALL WHERE rev_id IN ({','.join(str(s[0]) for s in st)})")
    meta = {r: (d, e, a) for r, d, e, a in cur.fetchall()}
    C.table(f"Talvisota, claim {cid}: its {len(st)} states (the claim with the most distinct texts)", ["From", "Editor", "Refs", "Text (start)"],
            [[str(meta[r][0]), "anonymous" if meta[r][2] else (meta[r][1] or "?"), nr, t[:150] + "…"] for r, nr, t in st[:14]])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", choices=("char", "sem"), default="char")
    configure(ap.parse_args().run)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s: dict = {}
    steps = [check_units, lambda: s.update(ids=load_ids(), events=load_events()), lambda: check_invariants(s["ids"], s["events"]),
             check_replay, lambda: check_restores(s["ids"]), lambda: check_accounting(s["ids"], s["events"]),
             lambda: s.update(texts=claim_texts(D)), lambda: check_rules(s["ids"], s["events"]),
             lambda: check_states(s["ids"]), lambda: check_golden(s["texts"])] + ([check_merge_changes] if RUN == "char" else []) + [
             lambda: check_sizes(s["ids"]), check_biography, check_mutations]
    for i, step in enumerate(steps):
        t = time.perf_counter()
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"{C.doc['feature']}.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
        print(f"  step {i}: {time.perf_counter() - t:.1f} s", flush=True)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
