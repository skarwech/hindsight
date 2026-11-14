"""F4.3: the stratified golden-set pool, drawn from the current lineage run, for two independent judges.

    .venv/bin/python eval/f4_pool.py               # character run: evidence/f4_pool.json and f4_pool_blind.json
    .venv/bin/python eval/f4_pool.py --run sem     # semantic run (Step 4): evidence/f4s_pool.json and f4s_pool_blind.json

Judged strata cover every similarity-based decision of the resolver (data/lineage/events.parquet `rule`):
  ratio 0.60–0.70 / 0.70–0.80 / 0.80–0.90 / 0.90–1.00, contain, near (revival of a near-duplicate)
and the decisions not to link: for each claim deleted in a revision, its most similar claim added in that revision
(the resolver's own score), same section in three bands below 0.60 and another section at 0.45 or more. Not covered:
non-links across revisions (a claim deleted earlier, an addition later), which F4.3c counts.
Not judged: moves, reverts and exact revivals, which link identical texts (an exact earlier state of the claim); their
risk is choosing among several claims that held the text, which the restore oracle (F4.4) exercises.
Each pair carries its stratum's population at draw time; ids are shuffled so the blind file leaks no stratum. The judges'
labels go to f4_labels.json as {"blind_sha256": <digest of f4_pool_blind.json>, "judges": [{judge, labels}]}, which binds
them to this pool. F4.3 scores the pool only while every pair is still in its stratum in the current run (strata()),
weighting by the current populations; when a resolver change moves a labelled pair, redraw and relabel.
The semantic run (Step 4) adds strata: semantic links in three cosine bands, near misses (an addition whose best
same-section candidate scores 0.80–0.88, not linked) and non-links across revisions (an addition whose best match is a
claim deleted up to LOOKBACK revisions earlier at ratio 0.60–0.90). Its pool is drawn with its own seed.
"""
import argparse, hashlib, json, random, sys
from collections import defaultdict

import duckdb
import numpy as np
from rapidfuzz import fuzz

from common import ROOT, embed

sys.path.insert(0, str(ROOT / "pipeline"))
from resolver import LOOKBACK, T_EDIT, T_NEAR, T_SEM, similarity  # noqa: E402

LIN, CH, EV = ROOT / "data/lineage", ROOT / "data/chunks", ROOT / "docs/validation/evidence"
PER_STRATUM = 18
SEEDS = {"char": 2028, "sem": 2031}   # 2027: the pool the containment rule was tuned on; 2030: semantic pool on copy vectors
SEM_BANDS = [(0.88, 0.92), (0.92, 0.96), (0.96, 1.0)]
MISS_BANDS = [(0.80, 0.84), (0.84, 0.88)]
RATIO_BANDS = [(0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
NOLINK_BANDS = [(0.0, 0.3), (0.3, 0.45), (0.45, 0.6)]


def digest(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def claim_texts(d: duckdb.DuckDBPyConnection) -> dict[tuple[int, int], dict[int, tuple[str, str, int]]]:
    """(page_id, ord) -> {claim_id: (text, section, text_hash)} for every non-hidden revision with chunks."""
    out: dict = defaultdict(dict)
    for p, o, c, t, s, h in d.execute(f"""SELECT i.page_id, i.ord, i.claim_id, v.text, r.section, r.text_hash
                                       FROM '{LIN}/claim_ids.parquet' i JOIN '{CH}/rev_chunks.parquet' r USING (page_id, rev_id, pos)
                                       JOIN '{CH}/chunk_variant.parquet' v USING (exact_hash)""").fetchall():
        out[(p, o)][c] = (t, s, h)
    return out


Row = tuple[int, int, str, str, str, float]               # (page_id, ord, old text, new text, section, score)


def band(x: float, bands: list[tuple[float, float]]) -> tuple[float, float] | None:
    return next((b for b in bands if b[0] <= x < b[1] or (b[1] == 1.0 and x >= b[0])), None)


def strata(d: duckdb.DuckDBPyConnection, texts: dict | None = None, vectors: dict | None = None) -> dict[str, list[Row]]:
    """Every judged decision of the current lineage run, by stratum. F4.3 re-derives these to confirm each labelled pair
    is still decided the same way, and takes the stratum populations (the weights) from the current run. With vectors
    (the semantic run) it adds the semantic, near-miss and across-revisions strata."""
    texts = claim_texts(d) if texts is None else texts
    ords: dict[int, list[int]] = defaultdict(list)
    for p, o in d.execute(f"SELECT page_id, ord FROM '{CH}/revisions.parquet' WHERE NOT hidden ORDER BY page_id, ord").fetchall():
        ords[p].append(o)
    prev_ord = {(p, o): b for p, os in ords.items() for b, o in zip([None] + os[:-1], os, strict=True)}
    last_text: dict[tuple[int, int], tuple[str, str, int]] = {}  # (page, claim) -> text when last seen alive
    died: dict[tuple[int, int], int] = {}                       # (page, dead claim) -> visible index of its deletion
    out: dict[str, list[Row]] = defaultdict(list)
    events = defaultdict(list)
    for p, o, kind, cid, sim, rule in d.execute(f"SELECT page_id, ord, kind, claim_id, sim, rule FROM '{LIN}/events.parquet'").fetchall():
        events[(p, o)].append((kind, cid, sim, rule))
    cos = lambda a, b: float(vectors[a] @ vectors[b])
    for p, os in ords.items():
        for k, o in enumerate(os):
            before, after = texts.get((p, prev_ord[(p, o)]), {}), texts.get((p, o), {})
            evs = events.get((p, o), [])
            for kind, cid, sim, rule in evs:
                if kind == "edit" and rule == "ratio" and sim < 1.0:
                    lo, hi = next(b for b in RATIO_BANDS if b[0] <= sim < b[1] or (b[1] == 1.0 and sim >= b[0]))
                    out[f"linked ratio {lo:.2f}–{hi:.2f}"].append((p, o, before[cid][0], after[cid][0], after[cid][1], sim))
                elif kind == "edit" and rule == "contain":
                    out["linked contain"].append((p, o, before[cid][0], after[cid][0], after[cid][1], sim))
                elif kind == "revive" and rule == "near":
                    out["linked near-duplicate revival"].append((p, o, last_text[(p, cid)][0], after[cid][0], after[cid][1], sim))
                elif kind == "semantic":
                    lo, hi = band(sim, SEM_BANDS)
                    old = before[cid][0] if cid in before else last_text[(p, cid)][0]
                    out[f"linked semantic {lo:.2f}–{hi:.2f}"].append((p, o, old, after[cid][0], after[cid][1], sim))
            dels = [cid for kind, cid, _, _ in evs if kind == "delete"]
            adds = [cid for kind, cid, _, _ in evs if kind == "add"]
            if dels and adds:
                score, _ = similarity([before[c][0] for c in dels], [after[c][0] for c in adds])
                for a, c in enumerate(dels):
                    same = [b for b, x in enumerate(adds) if after[x][1] == before[c][1]]
                    other = [b for b, x in enumerate(adds) if after[x][1] != before[c][1]]
                    if same:
                        b = max(same, key=lambda b: score[a, b]); s = float(score[a, b])
                        lo, hi = next(x for x in NOLINK_BANDS + [(0.6, 9.9)] if x[0] <= s < x[1])
                        out[f"not linked, same section {lo:.2f}–{min(hi, T_EDIT):.2f}" if hi <= T_EDIT else "not linked, same section >= 0.60"].append(
                            (p, o, before[c][0], after[adds[b]][0], before[c][1], s))
                    if other:
                        b = max(other, key=lambda b: score[a, b]); s = float(score[a, b])
                        if s >= 0.45:
                            out["not linked, other section >= 0.45"].append((p, o, before[c][0], after[adds[b]][0], before[c][1], s))
            if vectors is not None and adds:
                recent = [c for (q, c), at in died.items() if q == p and at == k - 1 and c not in after]
                olds = [(before[c][0], before[c][1], before[c][2]) for c in dels] + [last_text[(p, c)] for c in recent]
                for x in adds:                             # near miss: the best same-section candidate, below t_sem
                    t, sec, h = after[x]
                    cands = [(cos(oh, h), ot) for ot, os_, oh in olds if os_ == sec and oh in vectors and h in vectors]
                    if cands:
                        c_best, ot = max(cands)
                        b = band(c_best, MISS_BANDS)
                        if b:
                            out[f"not linked, semantic near miss {b[0]:.2f}–{b[1]:.2f}"].append((p, o, ot, t, sec, c_best))
                dead = [(c, last_text[(p, c)]) for (q, c), at in died.items() if q == p and k - at <= LOOKBACK and c not in after]
                if dead:                                   # across revisions: the best earlier-deleted match, below T_NEAR
                    for x in adds:
                        score = [(fuzz.ratio(v[0], after[x][0]) / 100, v) for _, v in dead]
                        r, v = max(score, key=lambda z: z[0])
                        if T_EDIT <= r < T_NEAR:
                            out["not linked, across revisions 0.60–0.90"].append((p, o, v[0], after[x][0], after[x][1], r))
            for c in set(before) - set(after):
                died[(p, c)] = k
            for c in set(after) - set(before):
                died.pop((p, c), None)
            for cid, v in after.items():
                last_text[(p, cid)] = v
    return out


def main() -> None:
    global LIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", choices=("char", "sem"), default="char")
    run = ap.parse_args().run
    sem = run == "sem"
    LIN = ROOT / ("data/lineage_sem" if sem else "data/lineage")
    pre = "f4s" if sem else "f4"
    vectors = None
    if sem:
        from embed_chunks import unit_vectors
        vectors = unit_vectors()
    d = duckdb.connect()
    strata_now = strata(d, vectors=vectors)
    titles = dict(d.execute(f"SELECT page_id::INT, title FROM read_csv('{ROOT}/docs/hero.tsv', delim='\t', header=true)").fetchall())
    rnd = random.Random(SEEDS[run])
    pool = []
    for name in sorted(strata_now):
        for p, o, old, new, sec, sim in rnd.sample(strata_now[name], min(PER_STRATUM, len(strata_now[name]))):
            pool.append(dict(stratum=name, population=len(strata_now[name]), page=titles.get(p, str(p)), page_id=p, ord=o,
                             section=sec or "(lead)", score=round(sim, 4), old=old, new=new))
    rnd.shuffle(pool)
    for i, x in enumerate(pool, 1):
        x["id"] = i
    v = np.array(embed([x["old"] for x in pool] + [x["new"] for x in pool])); v /= np.linalg.norm(v, axis=1, keepdims=True)
    for i, x in enumerate(pool):
        x["cosine"] = round(float(v[i] @ v[i + len(pool)]), 4)
    meta = dict(run=run, events_sha256=digest(LIN / "events.parquet"), t_edit=T_EDIT, t_sem=T_SEM if sem else None,
                per_stratum=PER_STRATUM, seed=SEEDS[run], populations={k: len(s) for k, s in strata_now.items()})
    (EV / f"{pre}_pool.json").write_text(json.dumps(dict(meta=meta, pairs=pool), ensure_ascii=False, indent=1), encoding="utf-8")
    blind = sorted(({k: x[k] for k in ("id", "page", "section", "old", "new")} for x in pool), key=lambda x: x["id"])
    (EV / f"{pre}_pool_blind.json").write_text(json.dumps(blind, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(pool), "pairs; populations:", meta["populations"])


if __name__ == "__main__":
    main()
