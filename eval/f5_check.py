"""F5 — embedding pipeline and `chunk` store (plan section 7, F5; gate H14).

    .venv/bin/python eval/f5_check.py            # writes docs/validation/f5.json (~6 min)

Needs the plan and store (pipeline/embed_plan.py, embed_chunks.py), the loaded `chunk` table (load_chunks.py), the kill
evidence (eval/f5_kill.py), the frozen probes and queries (evidence/f5_probes*.json, f5_queries.json, f5_frozen.json; the
targets follow eval/f5_probe_targets.py), Ollama, and sentence-transformers with BAAI/bge-m3 for the parity check.
HNSW results vary slightly between sessions (graph search order), so its numbers are one session's measurement.
"""
import hashlib, json, random, sys, time
from collections import Counter
from datetime import datetime, timezone

import duckdb
import numpy as np

from common import ROOT, Checks, connect, embed

sys.path.insert(0, str(ROOT / "pipeline"))
from chunker import text_hash  # noqa: E402
from embed_chunks import DIM, read_store  # noqa: E402
from embed_plan import digest  # noqa: E402

C = Checks("F5", "Embedding pipeline and chunk store", "H14")
CH, EMB, LIN, EV = ROOT / "data/chunks", ROOT / "data/embed", ROOT / "data/lineage", ROOT / "docs/validation/evidence"
CFG = json.loads((ROOT / "db/vector_search.json").read_text(encoding="utf-8"))
EF, EPS = CFG["mhnsw_ef_search"], 1e-6
D = duckdb.connect()


def db_table() -> tuple[np.ndarray, list[str], np.ndarray]:
    cur = connect("wikihistory").cursor()
    cur.execute("SELECT text_hash, text, emb FROM chunk ORDER BY text_hash")
    rows = cur.fetchall()
    vec = np.frombuffer(b"".join(bytes(r[2]) for r in rows), dtype="<f4").reshape(-1, DIM) if rows else np.zeros((0, DIM), "<f4")
    return np.array([r[0] for r in rows], dtype=np.uint64), [r[1] for r in rows], vec


def knn(cur, q: np.ndarray, k: int, exact: bool) -> list[tuple[int, float]]:
    b = np.ascontiguousarray(q, dtype="<f4").tobytes()
    hint = "IGNORE INDEX (emb)" if exact else ""
    cur.execute(f"SELECT text_hash, VEC_DISTANCE_COSINE(emb, ?) FROM chunk {hint} ORDER BY VEC_DISTANCE_COSINE(emb, ?) LIMIT {k}", (b, b))
    return [(int(h), float(d)) for h, d in cur.fetchall()]


def explain_key(cur, q: np.ndarray, exact: bool) -> str | None:
    b = np.ascontiguousarray(q, dtype="<f4").tobytes()
    cur.execute(f"EXPLAIN SELECT text_hash FROM chunk {'IGNORE INDEX (emb)' if exact else ''} ORDER BY VEC_DISTANCE_COSINE(emb, ?) LIMIT 10", (b,))
    cols = [c[0] for c in cur.description]
    return cur.fetchall()[0][cols.index("key")]


def unit(v: list[list[float]]) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a, axis=1, keepdims=True)


# ---------- F5.1 completeness, F5.2 hygiene ----------
def check_completeness(db: tuple) -> None:
    h, texts, vec = db
    want = dict(D.execute(f"SELECT text_hash, text FROM '{CH}/chunk_text.parquet'").fetchall())
    used = {x for (x,) in D.execute(f"SELECT DISTINCT text_hash FROM '{CH}/rev_chunks.parquet'").fetchall()}
    have = set(h.tolist())
    orphans, extra = len(used - have), len(have - used)
    text_ok = sum(want.get(int(x)) == t for x, t in zip(h, texts, strict=True))
    hash_ok = sum(text_hash(t) == int(x) for x, t in zip(h, texts, strict=True))
    sh, src, sv = read_store()
    plan = dict(D.execute(f"SELECT text_hash, action FROM '{EMB}/plan.parquet'").fetchall())
    meta = json.loads((EMB / "plan.json").read_text(encoding="utf-8"))
    plan_fresh = meta["plan_sha256"] == digest(EMB / "plan.parquet") and all(
        digest(p) == meta["inputs"][p.name] for p in (CH / "chunk_text.parquet", CH / "rev_chunks.parquet", LIN / "claim_ids.parquet", LIN / "states.parquet"))
    store_ok = len(sh) == len(set(sh.tolist())) == len(plan) and all(plan.get(int(x)) == s for x, s in zip(sh, src, strict=True))
    order = np.argsort(sh); same_vec = len(sh) == len(h) and bool(np.array_equal(sh[order], h)) and bool(np.array_equal(sv[order], vec))
    ok = orphans == 0 and extra == 0 and text_ok == hash_ok == len(h) == len(used) and store_ok and same_vec and plan_fresh
    C.add("F5.1", "Completeness: every chunk hash the chunker produced has exactly one row in `chunk`, with its text and the store's vector",
          "invariant", "0 orphans both ways; text = the embedded spelling and re-hashes to its key; store = plan (one row per hash, "
          "model/copy as planned, plan fresh); table vectors byte-identical to the store",
          f"{len(h):,} rows for {len(used):,} chunk hashes: {orphans} orphans, {extra} extra; text matches {text_ok:,}, re-hashes {hash_ok:,}; "
          f"store = plan {store_ok} ({src.count('model'):,} model, {src.count('copy'):,} copy); plan fresh {plan_fresh}; vectors identical {same_vec}", ok)


def check_hygiene(db: tuple) -> None:
    h, _, vec = db
    finite = bool(np.isfinite(vec).all()); norms = np.linalg.norm(vec, axis=1)
    cur = connect("wikihistory").cursor()
    cur.execute("SELECT MAX(VEC_DISTANCE_COSINE(emb, emb)), COUNT(*) FROM chunk"); self_max, n = cur.fetchone()
    distinct = len({v.tobytes() for v in vec})
    C.add("F5.2", "Vector hygiene: no NaN or Inf, unit norms, no two chunks share a vector (no copies)",
          "invariant", f"all finite; norms within 1e-3 of 1; distinct vectors = rows; the server's distance of each row to itself < {EPS} "
          "(smoke: it is 0 for any non-zero vector)",
          f"finite {finite}; norms {norms.min():.6f}–{norms.max():.6f}; {distinct:,} distinct vectors for {len(h):,} rows; max self distance "
          f"{self_max:.2e} over {n:,} rows",
          finite and abs(norms.min() - 1) < 1e-3 and abs(norms.max() - 1) < 1e-3 and self_max < EPS and distinct == len(h))


# ---------- F5.3 self-retrieval ----------
def check_self(db: tuple) -> None:
    h, _, vec = db
    cur = connect("wikihistory").cursor(); cur.execute(f"SET mhnsw_ef_search = {EF}")
    pick = random.Random(41).sample(range(len(h)), 200)
    res: dict[str, int] = Counter(); ties = 0
    for k in pick:
        for exact in (True, False):
            r = knn(cur, vec[k], 10, exact)
            top = [x for x, d in r if d <= r[0][1] + EPS]
            hit = int(h[k]) in top or (len(top) == len(r) and abs(r[0][1]) <= EPS)   # 10+ exact duplicates at distance 0
            res["exact" if exact else "hnsw"] += hit; ties += exact and len(top) > 1
    keys = (explain_key(cur, vec[pick[0]], False), explain_key(cur, vec[pick[0]], True))
    C.add("F5.3", "Self-retrieval: 200 random chunks find themselves at rank 1 (exact duplicates at distance 0 would count as ties)",
          "invariant", "exact search (IGNORE INDEX) 200 of 200; HNSW >= 99%; EXPLAIN shows the vector index on the HNSW path only",
          f"exact {res['exact']} of 200, HNSW {res['hnsw']} of 200 (ef {EF}); {ties} queries had rank-1 ties; EXPLAIN key: HNSW {keys[0]!r}, "
          f"exact {keys[1]!r}", res["exact"] == 200 and res["hnsw"] >= 198 and keys[0] == "emb" and keys[1] is None)


# ---------- F5.4 parity, F5.5 shortcut oracle ----------
def check_parity(db: tuple) -> None:
    from sentence_transformers import SentenceTransformer
    h, texts, vec = db
    model_rows = {x for (x,) in D.execute(f"SELECT text_hash FROM '{EMB}/plan.parquet' WHERE action = 'model'").fetchall()}
    idx = [k for k in np.argsort([-len(t) for t in texts]) if int(h[k]) in model_rows]
    pick = idx[:10] + random.Random(43).sample(idx[10:], 40)
    m = SentenceTransformer("BAAI/bge-m3", device="mps"); m.max_seq_length = 8192
    st = m.encode([texts[k] for k in pick], batch_size=8, normalize_embeddings=True)
    cos = (st * vec[pick]).sum(axis=1)
    C.add("F5.4", "T5 parity on real chunks: sentence-transformers bge-m3 (fp32, max_seq_length 8192) vs the stored Ollama vectors, 50 chunks "
          "(the 10 longest and 40 random)", "oracle", "min cosine >= 0.99", f"min {cos.min():.5f}, median {np.median(cos):.5f}", bool(cos.min() >= 0.99))


def check_shortcut(db: tuple) -> None:
    """The plan's shortcut (copy the parent's vector when the text is >= 0.97 similar) measured on every would-be copy: each
    one's own fresh vector (now in the store) against its anchor's. It failed, so the store embeds every chunk."""
    from embed_plan import COPY_RATIO, decide
    h, _, vec = db
    row = {int(x): k for k, x in enumerate(h)}
    from rapidfuzz import fuzz
    text = dict(zip(h.tolist(), db[1], strict=True))
    cols = decide(0.97)
    pairs = [(c, a, r) for c, act, a, r in zip(cols["text_hash"], cols["action"], cols["anchor"], cols["ratio"], strict=True) if act == "copy"]
    # the plan's literal rule, one hop: copy the parent's own vector whenever the text is >= 0.97 similar to the parent's
    literal = [(c, q) for c, q in zip(cols["text_hash"], cols["parent"], strict=True)
               if q is not None and fuzz.ratio(text[c], text[q]) / 100 >= 0.97]
    lit = np.array([float(vec[row[c]] @ vec[row[q]]) for c, q in literal])
    cos = np.array([float(vec[row[c]] @ vec[row[a]]) for c, a, _ in pairs]); ratio = np.array([r for *_, r in pairs])
    share = float((cos >= 0.98).mean())
    C.table("Copy shortcut (plan: copy the parent's vector at character ratio >= 0.97): cosine between each would-be copy's own fresh "
            "vector and its anchor's", ["Character ratio to the anchor", "Would-be copies", "cos >= 0.98", "Share", "Min cosine", "Median"],
            [[f"{lo:.2f}–{hi:.2f}", int(m.sum()), int((cos[m] >= 0.98).sum()), round(float((cos[m] >= 0.98).mean()), 4),
              round(float(cos[m].min()), 4), round(float(np.median(cos[m])), 4)]
             for lo, hi in ((0.97, 0.98), (0.98, 0.99), (0.99, 1.001)) for m in [(ratio >= lo) & (ratio < hi)] if m.any()])
    C.add("F5.5", f"Copy shortcut oracle on all {len(pairs):,} would-be copies (plan: 200): a fresh embedding agrees with the copied vector",
          "oracle", "plan: cosine >= 0.98 for >= 99% of copies; recorded here because the result turned the shortcut off",
          f"anchor rule (embed_plan: ratio to the chain's model row, copy its vector): {share:.2%} of {len(pairs):,} at >= 0.98 (min "
          f"{cos.min():.4f}, P1 {np.quantile(cos, 0.01):.4f}); even at ratio >= 0.99 only {float((cos[ratio >= 0.99] >= 0.98).mean()):.1%}. "
          f"Plan-literal rule (parent's vector, one hop): {float((lit >= 0.98).mean()):.2%} of {len(literal):,}. Worst: name-swap vandalism that "
          f"keeps 97% of the characters. Shortcut off (COPY_RATIO {COPY_RATIO})", None)
    plan = D.execute(f"SELECT COUNT(*) FILTER (WHERE action = 'copy'), COUNT(*) FROM '{EMB}/plan.parquet'").fetchall()[0]
    _, src, _ = read_store()
    pick = random.Random(45).sample(range(len(h)), 300)
    fresh = unit(embed([db[1][k] for k in pick]))
    agree = (fresh * vec[pick]).sum(axis=1)
    C.add("F5.5b", "Vector provenance: every stored vector is the model's embedding of its own text (no copies)", "invariant",
          "0 copies in the plan and the store; 300 random rows re-embedded now match the stored vector (cosine >= 0.9999)",
          f"{plan[0]} copies in the plan of {plan[1]:,}; store sources {dict(Counter(src))}; re-embedded min cosine {agree.min():.6f}",
          plan[0] == 0 and set(src) == {"model"} and bool(agree.min() >= 0.9999))


# ---------- F5.6 probes, F5.7 ANN recall ----------
def check_frozen() -> bool:
    fz = json.loads((EV / "f5_frozen.json").read_text(encoding="utf-8"))
    return all(hashlib.sha256((EV / n).read_bytes()).hexdigest() == fz[n] for n in fz if n.endswith(".json"))


def claims_of_hashes() -> dict[int, set]:
    out: dict[int, set] = {}
    for th, p, c in D.execute(f"SELECT DISTINCT text_hash, page_id, claim_id FROM '{LIN}/states.parquet'").fetchall():
        out.setdefault(th, set()).add((p, c))
    return out


def probe_ranks(db: tuple, probes: list[dict]) -> list[dict]:
    """For each probe, where its target claim lands under four rankings: (1) every chunk in `chunk` (the plan's check, HNSW and
    exact), (2) distinct claims over all history, (3) paragraphs alive in the latest revision of all 60 pages (exact cosine
    over that time slice), (4) the same within the target's own page: oracle routing, i.e. the gold page as if routing were
    perfect. A target counts in any of its versions; one with no version in a pool is a miss there."""
    h, texts, vec = db
    row = {int(x): k for k, x in enumerate(h)}
    claims_of = claims_of_hashes()
    cur_page: dict[int, set] = {}
    for p, th in D.execute(f"""WITH last AS (SELECT page_id lp, max(ord) o FROM '{CH}/revisions.parquet' WHERE NOT hidden AND n_chunks > 0 GROUP BY 1)
                               SELECT r.page_id, r.text_hash FROM '{CH}/rev_chunks.parquet' r JOIN last ON lp = r.page_id AND o = r.ord""").fetchall():
        cur_page.setdefault(p, set()).add(th)
    all_cur = sorted({t for v in cur_page.values() for t in v})
    qv = unit(embed([p["question"] for p in probes]))
    con = connect("wikihistory").cursor(); con.execute(f"SET mhnsw_ef_search = {EF}")
    out = []
    for p, q in zip(probes, qv, strict=True):
        tgt = (p["page_id"], p["claim_id"]); hit = lambda th: tgt in claims_of.get(th, ())
        r = {}
        for exact in (False, True):
            got = [x for x, _ in knn(con, q, 5, exact)]
            r["exact" if exact else "hnsw"] = next((i + 1 for i, x in enumerate(got) if hit(x)), None)
            if not exact:
                r["top1"] = texts[row[got[0]]][:90]
        sims = vec @ q; seen: list = []
        for k in np.argsort(-sims):
            seen += [c for c in sorted(claims_of.get(int(h[k]), ())) if c not in seen]
            if len(seen) >= 5:
                break
        r["distinct"] = tgt in seen[:5]

        def rank_in(pool: list[int]) -> int | None:
            best = max((sims[row[x]] for x in pool if hit(x)), default=None)
            return None if best is None else 1 + int(sum(sims[row[t]] > best for t in pool))
        r["current"] = rank_in(all_cur); r["page"] = rank_in(sorted(cur_page.get(p["page_id"], ())))
        out.append(r)
    return out


SHAPES = [("All chunks, HNSW (the plan's check)", lambda r: r["hnsw"] is not None),
          ("Distinct claims, all history", lambda r: r["distinct"]),
          ("Alive now, all 60 pages (exact)", lambda r: r["current"] is not None and r["current"] <= 5),
          ("Alive now, gold page only (oracle routing)", lambda r: r["page"] is not None and r["page"] <= 5)]


def check_probes(db: tuple) -> None:
    from f4_check import wilson_lower
    from f5_probe_targets import follows_rules
    orig = json.loads((EV / "f5_probes.json").read_text(encoding="utf-8"))
    fresh = json.loads((EV / "f5_probes_fresh.json").read_text(encoding="utf-8"))
    ro, rf = probe_ranks(db, orig), probe_ranks(db, fresh)
    rows = [[p["probe"], p["lang"], p["question"], p["page"], r["hnsw"] or "—", r["exact"] or "—", r["current"] or "—", r["page"] or "—", r["top1"]]
            for p, r in zip(orig, ro, strict=True)]
    C.table("Golden probes, original 10 (model-written, frozen before retrieval): rank of the target paragraph (any version)",
            ["#", "Lang", "Question", "Target page", "All chunks, HNSW", "All chunks, exact", "Alive now, 60 pages", "Gold page (oracle)",
             "HNSW top hit (start)"], rows)
    frozen, rules = check_frozen(), follows_rules()
    n = sum(r["hnsw"] is not None for r in ro)
    C.add("F5.6", "Golden probes (the plan's check): 10 questions in fi, sv, en and fr find their paragraph in the top 5 of `chunk`",
          "golden", "at least 9 of 10 (HNSW over every chunk); probes frozen before retrieval; model-written, not hand-written; targets "
          "follow eval/f5_probe_targets.py", f"HNSW {n} of 10, exact {sum(r['exact'] is not None for r in ro)} of 10; frozen {frozen}; targets "
          f"follow the rule {all(rules.values())}. Misses: older versions of other paragraphs fill the top 5 (history flooding), or a related "
          "page wins; see F5.6b", n >= 9 and frozen and all(rules.values()))
    trows = []
    for name, ok in SHAPES:
        k_o, k_f = int(sum(bool(ok(r)) for r in ro)), int(sum(bool(ok(r)) for r in rf))
        by = Counter(p["lang"] for p, r in zip(fresh, rf, strict=True) if ok(r))
        trows.append([name, k_o, k_f, round(k_f / len(rf), 3), round(wilson_lower(k_f / len(rf), len(rf)), 3), k_o + k_f,
                      round(wilson_lower((k_o + k_f) / (len(ro) + len(rf)), len(ro) + len(rf)), 3)] + [by[l] for l in ("fi", "sv", "en", "fr")])
    C.table("Probes in the top 5 under each ranking: original 10 (seen while diagnosing) and fresh 40 (written after, frozen before retrieval)",
            ["Ranking", "Original /10", "Fresh /40", "Fresh share", "Fresh 95% lower bound", "All /50", "All 95% lower bound",
             "fi /10", "sv /10", "en /10", "fr /10"], trows)
    C.add("F5.6b", "Probes under the plan's ranking and the design's (rank what is alive at the date): every target is a current "
          "paragraph, so no probe tests a past date", "golden", "recorded: which ranking the 9-of-10 bar needs; the time-slice numbers use "
          "exact cosine over the slice, which Step 6 must then also use",
          "; ".join(f"{r[0]}: original {r[1]}/10, fresh {r[2]}/40 (lower bound {r[4]:.0%})" for r in trows), None)


def recall(cur, qs: np.ndarray) -> tuple[float, float, float, list[float]]:
    """Tie-aware recall@10 of HNSW against exact search, and median latencies (ms)."""
    rec, t_ann, t_ex = [], [], []
    for q in qs:
        t = time.perf_counter(); ex = knn(cur, q, 10, True); t_ex.append(time.perf_counter() - t)
        t = time.perf_counter(); an = knn(cur, q, 10, False); t_ann.append(time.perf_counter() - t)
        th = ex[-1][1] + EPS
        rec.append(sum(d <= th for _, d in an) / 10 if len({x for x, _ in an}) == len(an) == 10 else 0.0)
    return float(np.mean(rec)), 1000 * float(np.median(t_ann)), 1000 * float(np.median(t_ex)), rec


def check_recall(db: tuple) -> None:
    h, texts, vec = db
    claims_of = claims_of_hashes()
    qs = json.loads((EV / "f5_queries.json").read_text(encoding="utf-8"))
    qv = unit(embed([q["query"] for q in qs]))
    cv = vec[random.Random(47).sample(range(len(vec)), 200)]
    cur = connect("wikihistory").cursor()
    claims = lambda res: set().union(*(claims_of.get(x, set()) for x, _ in res))
    exact_top = {i: knn(cur, q, 10, True) for i, q in enumerate(qv)}
    rows, lost_at = [], {}
    for ef in sorted({20, 40, EF, 160, 400}):
        cur.execute(f"SET mhnsw_ef_search = {ef}")
        rq, lat, lat_ex, per = recall(cur, qv)
        rc, _, _, _ = recall(cur, cv)
        lost = [i for i, q in enumerate(qv) if not claims(exact_top[i]) & claims(knn(cur, q, 10, False))]
        lost_at[ef] = lost
        rows.append([ef, round(rq, 4), sum(x == 0 for x in per), len(lost), round(rc, 4), round(lat, 2), round(lat_ex, 2)])
        if ef == EF:
            gate, chunk_rec, ms, ms_ex = rq, rc, lat, lat_ex
    langs = Counter(q["lang"] for q in qs)
    C.table("ANN recall@10 against exact search (tie-aware) by mhnsw_ef_search, one session; 'answer lost' = none of the claims behind "
            "the exact top 10 appears in the HNSW top 10", ["ef_search", "Recall, 200 questions", "Questions at 0% recall",
            "Answer lost (claims)", "Recall, 200 chunk vectors", "HNSW ms", "Exact ms"], rows)
    lost_txt = "; ".join(f"{qs[i]['lang']}: {qs[i]['query'][:60]}" for i in lost_at[EF])
    C.add("F5.7", f"ANN recall@10 against exact search on 200 question-style queries ({', '.join(f'{k} {v}' for k, v in langs.items())}), "
          f"M=16, ef_search {EF} (db/vector_search.json, set before measuring)", "oracle",
          "recall >= 95% on the questions (the plan's gate); recorded: queries whose whole answer HNSW loses, and chunk-vector queries "
          "(near-trivial: the query is in the index)",
          f"questions {gate:.2%}; answer lost on {len(lost_at[EF])} of 200 at ef {EF}, {len(lost_at[160])} at 160, {len(lost_at[400])} at 400 "
          f"(the misses are a few topics asked in several languages, whose answers sit in tight clusters of paragraph versions); chunk vectors "
          f"{chunk_rec:.2%}; HNSW {ms:.1f} ms vs exact {ms_ex:.1f} ms; queries unchanged since frozen: {check_frozen()}",
          gate >= 0.95 and check_frozen(), f"answer lost at ef {EF}: {lost_txt}")


# ---------- F5.8 resumability, F5.9 full text, F5.10 sizes ----------
def check_resume(n_rows: int) -> None:
    from embed_chunks import store_digest
    ek = json.loads((EV / "f5_kill.json").read_text(encoding="utf-8"))
    lk = json.loads((EV / "f5_load_kill.json").read_text(encoding="utf-8"))
    sha = lambda f: hashlib.sha256((ROOT / "pipeline" / f).read_bytes()).hexdigest()
    plan_sha, store_sha = json.loads((EMB / "plan.json").read_text(encoding="utf-8"))["plan_sha256"], store_digest()
    bound = dict(embedding=(ek.get("plan_sha256"), ek.get("store_sha256"), ek.get("code_sha256")) == (plan_sha, store_sha, sha("embed_chunks.py")),
                 load=(lk.get("plan_sha256"), lk.get("store_sha256"), lk.get("code_sha256")) == (plan_sha, store_sha, sha("load_chunks.py")))
    r = ek.get("restart") or {}
    emb_ok = (ek["after_kill"]["returncode"] == -9 and ek["restart_returncode"] == 0 and r.get("complete")
              and r.get("rows_before") == ek["after_kill"]["shards"] * 512 and r.get("plan_sha256") == plan_sha)
    load_ok = lk["after_kill"]["returncode"] == -9 and 0 < lk["after_kill"]["rows"] < n_rows and lk["restart"]["rows_after"] == n_rows
    C.add("F5.8", "Resumability: the real embedding job (from an empty store) and the real load (into an empty table) were each "
          "SIGKILLed mid-run and restarted; the evidence is bound to the current plan, store and code",
          "invariant", "both finish after the restart; evidence records this plan's sha256, this store's content digest and these jobs' code "
          "hashes; F5.1 checks the store and table (a torn shard write and its cleanup: pipeline/test_embed_chunks.py)",
          f"embedding: killed after {ek['after_kill']['shards']} whole shards ({ek['delay_s']} s into the next batch), restart resumed at "
          f"{r.get('rows_before', 0):,} rows, complete {r.get('complete')}; load: killed at {lk['after_kill']['rows']:,} rows, restart inserted "
          f"{lk['restart']['inserted']:,}, final {lk['restart']['rows_after']:,}; bound to plan/store/code: {bound}",
          bool(emb_ok and load_ok and all(bound.values())), json.dumps(dict(embedding=ek, load=lk))[:5000])


def check_fulltext() -> None:
    cur = connect("wikihistory").cursor(); res = []
    for term in ("Mannerheim", "Kekkonen", "rauhansopimus", "Moskovan", "eduskunta"):
        cur.execute("SELECT text FROM chunk WHERE MATCH(text) AGAINST(? IN NATURAL LANGUAGE MODE) LIMIT 50", (term,))
        rows = [t for (t,) in cur.fetchall()]
        res.append((term, len(rows), sum(term.lower() in t.lower() for t in rows)))
    C.add("F5.9", "FULLTEXT index on chunk.text answers MATCH queries (the lexical signal R3 needs)", "smoke",
          "each of 5 terms returns rows (smoke: a token match always contains the term)", "; ".join(f"{t}: {n} rows, {c} contain it" for t, n, c in res),
          all(n > 0 and c == n for _, n, c in res))


def check_sizes() -> None:
    """The final build only: logs/embed.jsonl and load_chunks.jsonl hold this build's runs (earlier ones are in logs/superseded/);
    the killed runs never log, so their share comes from the kill evidence. Sizes are the table's files in the data dir."""
    emb = [json.loads(x) for x in (ROOT / "logs/embed.jsonl").read_text(encoding="utf-8").splitlines()]
    load = [json.loads(x) for x in (ROOT / "logs/load_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    ek = json.loads((EV / "f5_kill.json").read_text(encoding="utf-8"))
    model_s = sum(e["seconds"] for e in emb if e["model_embedded"]); n_model = sum(e["model_embedded"] for e in emb)
    data_dir = ROOT / ".mariadb/data/wikihistory"
    mb = lambda pattern: sum(f.stat().st_size for f in data_dir.glob(pattern)) / 2**20
    C.add("F5.10", "Throughput and size (the final build)", "smoke", "~9 chunks/s (plan est.; < 3/s means the num_ctx trap); index build "
          "~3.5 min per 28k vectors; 4 KB per vector",
          f"restart run: {n_model:,} chunks at {n_model / max(model_s, 1e-9):.1f}/s; whole job incl. the killed part {ek['seconds']:.0f} s for "
          f"30,946 chunks ({30946 / max(ek['seconds'], 1e-9):.1f}/s with the restart); load {sum(x['seconds'] for x in load):.0f} s for the "
          f"restart half (vector index built during the inserts); files: data {mb('chunk.ibd'):.0f} MB, vector index {mb('chunk#i#*.ibd'):.0f} MB "
          f"({mb('chunk#i#*.ibd') * 2**20 / 30946 / 1024:.1f} KB per vector), full-text index files {mb('FTS_*_INDEX_*.ibd'):.0f} MB",
          None, json.dumps(dict(embed=emb, load=load)))


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s: dict = {}
    steps = [lambda: s.update(db=db_table()), lambda: check_completeness(s["db"]), lambda: check_hygiene(s["db"]),
             lambda: check_self(s["db"]), lambda: check_parity(s["db"]), lambda: check_shortcut(s["db"]),
             lambda: check_probes(s["db"]), lambda: check_recall(s["db"]), lambda: check_resume(len(s["db"][0])),
             check_fulltext, check_sizes]
    for i, step in enumerate(steps):
        t = time.perf_counter()
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"F5.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
        print(f"  step {i}: {time.perf_counter() - t:.1f} s", flush=True)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
