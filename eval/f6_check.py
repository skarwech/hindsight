"""F6 — deep-layer load and reconstruction (plan section 7, F6; gate H14).

    .venv/bin/python eval/f6_check.py            # writes docs/validation/f6.json (~5 min; reloads a copy into `smoke`)

Needs claim, layout and text_variant loaded (pipeline/load_claims.py), page_state and chunk in wikihistory, and the fault
injection evidence (eval/f6_mutate.py). Reconstruction goes through SQL AS OF only; expected lists come from the chunker
(data/chunks/rev_chunks.parquet) and the revision active at a time from page_state.
"""
import hashlib, json, random, statistics, subprocess, sys, time
from collections import Counter
from datetime import datetime, timedelta, timezone

import duckdb

import f6_lib as L
from common import ROOT, Checks, connect

C = Checks("F6", "Deep-layer load and reconstruction", "H14")
EV = ROOT / "docs/validation/evidence"
D = duckdb.connect()


def titles() -> dict[int, str]:
    return dict(D.execute(f"SELECT page_id::INT, title FROM read_csv('{ROOT}/docs/hero.tsv', delim='\t', header=true)").fetchall())


# ---------- F6.0 provenance ----------
def check_provenance() -> None:
    log = json.loads((ROOT / "logs/load_claims_wikihistory.json").read_text(encoding="utf-8"))
    lin = json.loads((ROOT / "logs/lineage.json").read_text(encoding="utf-8"))
    from load_claims import INPUTS, digest
    same = {p.name: digest(p) == log["inputs"][p.name] for p in INPUTS}
    resolver = hashlib.sha256((ROOT / "pipeline/resolver.py").read_bytes()).hexdigest()
    char_run = not lin.get("semantic_pass") and "t_sem" not in lin and lin["resolver_sha256"] == log["lineage_log"] == resolver
    C.add("F6.0", "Provenance: the load read the character-only lineage that F4 validated, and its inputs have not changed since",
          "invariant", "the load log's input digests equal the current files; data/lineage is the character run (no semantic pass)",
          f"inputs unchanged {same}; character run {char_run} (resolver {lin['resolver_sha256'][:12]}…); {len(log['runs'])} load run(s) since "
          f"--fresh: {sum(r['pages_loaded'] for r in log['runs'])} pages, {sum(r['claim_rows'] for r in log['runs']):,} claim rows, "
          f"{sum(r['layout_rows'] for r in log['runs']):,} layout rows",
          all(same.values()) and char_run)


# ---------- F6.1 round trip, F6.2 layout = claims ----------
def check_round_trip(revs: dict, exp: dict) -> None:
    cur = connect("wikihistory").cursor()
    res: dict[str, Counter] = {k: Counter() for k in ("at", "before", "hidden", "instants")}
    bad: list[str] = []; sets_bad = 0; ok_at: dict[tuple[int, int], bool] = {}
    for p, rs in revs.items():
        prev: list = []
        for o, r, hidden, t in rs:
            for kind, when, want in (("before", t - L.ONE_US, prev), ("hidden" if hidden else "at", t, exp[(p, r)])):
                got, lay, alive = L.reconstruct(cur, "wikihistory", p, when)
                ok = got == want; res[kind]["n"] += 1; res[kind]["ok"] += ok; sets_bad += lay != alive
                ok_at[(p, r)] = ok_at.get((p, r), True) and ok
                if not ok and len(bad) < 20:
                    bad.append(f"page {p} rev {r} {kind}: got {len(got)} chunks, want {len(want)}")
            prev = exp[(p, r)]
    rnd = random.Random(61); pages = sorted(revs); ps = connect("wikihistory").cursor()
    times = {(p, t) for p, rs in revs.items() for *_, t in rs}
    while res["instants"]["n"] < 1000:                    # (c) random instants that are not revision times
        p = rnd.choice(pages); lo, hi = revs[p][0][3], revs[p][-1][3] + timedelta(days=30)
        t = lo + timedelta(microseconds=rnd.randrange(int((hi - lo).total_seconds() * 1e6)))
        if (p, t) in times:
            continue
        ps.execute("SELECT rev_id FROM page_state FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?", (L.ts_text(t), p))
        r = ps.fetchone()[0]
        got, lay, alive = L.reconstruct(cur, "wikihistory", p, t)
        ok = got == exp[(p, r)]; res["instants"]["n"] += 1; res["instants"]["ok"] += ok; sets_bad += lay != alive
        if not ok and len(bad) < 20:
            bad.append(f"page {p} instant {t}: active rev {r}")
    big = sorted(revs, key=lambda p: -len(revs[p]))[:3]
    sample = rnd.sample([(p, r) for p, rs in revs.items() for _, r, h, _ in rs if not h], 1000)
    sample_ok = sum(ok_at[x] for x in sample)
    big_n = sum(len(revs[p]) for p in big); big_ok = sum(ok_at[(p, r)] for p in big for _, r, _, _ in revs[p])
    name = titles()
    revision_count, chunk_count_bad = L.chunk_count_audit()
    C.add("F6.1", "Round trip (main gate): every article rebuilt through AS OF equals the chunker's chunk list, exact spelling included",
          "invariant", "(a) the plan's 1,000 random revisions, here every revision at its own time and 1 µs before (the predecessor, or "
          "empty before the first); (b) every revision of the 3 largest pages; (c) 1,000 random instants that are not revision times, "
          "against the revision page_state says is active: all 100%; every revisions.parquet n_chunks equals the emitted chunk count",
          f"(a) at revision time {res['at']['ok']:,}/{res['at']['n']:,} visible revisions, of which the plan's random 1,000: {sample_ok}/1000; "
          f"1 µs before {res['before']['ok']:,}/{res['before']['n']:,}; (b) the 3 largest pages ({', '.join(name.get(p, str(p)) for p in big)}): "
          f"{big_ok:,}/{big_n:,} revisions at and before; (c) random instants {res['instants']['ok']}/{res['instants']['n']}; hidden revisions "
          f"(expected = the carried-over list, not an independent source) {res['hidden']['ok']}/{res['hidden']['n']}; "
          f"n_chunks cross-check {revision_count - chunk_count_bad:,}/{revision_count:,} revisions",
          all(v["ok"] == v["n"] for v in res.values()) and chunk_count_bad == 0,
          ("\n".join(bad) or "no round-trip mismatch") + (f"; n_chunks mismatches: {chunk_count_bad}" if chunk_count_bad else ""))
    C.add("F6.2", "Layout and claim set equality: at every reconstructed moment the ids in claim_order are exactly the claims alive",
          "invariant", "0 differences (the plan: 1,000 random t; here every reconstruction above)",
          f"{sets_bad} differences in {sum(v['n'] for v in res.values()):,} reconstructions", sets_bad == 0)


# ---------- F6.3 integrity ----------
def check_integrity(revs: dict) -> None:
    bad = L.integrity("wikihistory")
    reb, sto = Counter(L.rebuilt_claim_rows(revs)), Counter(L.stored_claim_rows("wikihistory"))
    diff = sum((reb - sto).values()) + sum((sto - reb).values())
    C.add("F6.3a", "History integrity SQL: periods, current rows, page_state timestamps, metadata, hashes and layout changes",
          "invariant", "0 SQL integrity violations",
          f"{sum(bad.values())} violations {dict((k, v) for k, v in bad.items() if v) or ''}",
          sum(bad.values()) == 0, json.dumps(bad, indent=1))
    C.add("F6.3b", "Independent claim-history rebuild",
          "invariant", "claim rows equal a rebuild from claim ids + chunks, not the states file the loader read",
          f"claim rows {sum(sto.values()):,}, rebuilt {sum(reb.values()):,}, differing {diff}", diff == 0,
          "The event log tracks lineage events, but a claim state also opens when exact spelling, citation count, or section changes. "
          "Rebuilding from claim ids and chunk output covers those state changes independently.")


# ---------- F6.4 idempotence ----------
def check_idempotence() -> None:
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "pipeline/load_claims.py"), "--schema", "smoke", *a],
                                    capture_output=True, text=True)
    fresh = run("--fresh"); a = L.checksum("smoke")
    again = run(); b = L.checksum("smoke")
    live = L.checksum("wikihistory")
    resumed = json.loads(again.stdout.strip().splitlines()[-1]) if again.returncode == 0 else {}
    ok = fresh.returncode == 0 and again.returncode == 0 and a == live == b and resumed.get("pages_loaded") == 0
    C.add("F6.4", "Idempotence: a fresh reload into a clean schema (smoke) gives identical rows; re-running it adds nothing",
          "invariant", "identical row counts and sha256 over claim (page_id, claim_id, text_hash, exact_hash, row_start, row_end) and "
          "layout (page_id, rev_id, claim_order, row_start, row_end); the re-run loads 0 pages",
          f"claim {live['claim_rows']:,} rows, layout {live['layout_rows']:,}; fresh copy identical {a == live}; re-run loaded "
          f"{resumed.get('pages_loaded')} pages, unchanged {a == b}", ok, fresh.stderr[-1500:] + again.stderr[-500:])


# ---------- F6.5 storage ----------
def check_storage() -> None:
    starter = [p for p, why in D.execute(f"SELECT page_id::INT, why FROM read_csv('{ROOT}/docs/hero.tsv', delim='\t', header=true)").fetchall()
               if why.startswith("starter")]
    ids = ",".join(map(str, starter))
    full = D.execute(f"SELECT SUM(strlen(text)), COUNT(*) FROM '{ROOT}/data/m/versions-*.parquet' WHERE page_id IN ({ids})").fetchall()[0]
    clean = D.execute(f"""SELECT SUM(strlen(v.text)) FROM '{ROOT}/data/chunks/rev_chunks.parquet' r
                           JOIN '{ROOT}/data/chunks/chunk_variant.parquet' v USING (exact_hash)
                           WHERE r.page_id IN ({ids})""").fetchone()[0] or 0
    clean_versions = D.execute(f"SELECT COUNT(*) FROM '{ROOT}/data/chunks/revisions.parquet' WHERE page_id IN ({ids}) AND NOT hidden").fetchone()[0]
    cur = connect("wikihistory").cursor()
    q = lambda sql: (cur.execute(sql), cur.fetchone()[0])[1] or 0
    claim_b = q(f"""SELECT SUM(42 + COALESCE(LENGTH(section), 0) + COALESCE(LENGTH(editor), 0) + COALESCE(LENGTH(comment), 0))
                    FROM claim FOR SYSTEM_TIME ALL WHERE page_id IN ({ids})""")
    layout_b = q(f"SELECT SUM(28 + LENGTH(claim_order)) FROM layout FOR SYSTEM_TIME ALL WHERE page_id IN ({ids})")
    text_b = q(f"""SELECT SUM(LENGTH(k.text)) FROM chunk k WHERE k.text_hash IN
                   (SELECT DISTINCT text_hash FROM claim FOR SYSTEM_TIME ALL WHERE page_id IN ({ids}))""")
    var_b = q(f"""SELECT SUM(LENGTH(v.text)) FROM text_variant v WHERE v.exact_hash IN
                  (SELECT DISTINCT exact_hash FROM claim FOR SYSTEM_TIME ALL WHERE page_id IN ({ids}))""")
    n_vec = q(f"SELECT COUNT(DISTINCT text_hash) FROM claim FOR SYSTEM_TIME ALL WHERE page_id IN ({ids})")
    mb = lambda b: b / 2**20
    text_total = claim_b + layout_b + text_b + var_b
    stored_text = text_b + var_b
    files = L.table_files_mb("wikihistory")
    vec_files = sum(f.stat().st_size for f in (ROOT / ".mariadb/data/wikihistory").glob("chunk#i#*.ibd")) / 2**20
    fts_files = list((ROOT / ".mariadb/data/wikihistory").glob("FTS_*_INDEX_*.ibd"))
    fts_mb = sum(f.stat().st_size for f in fts_files) / 2**20
    C.table("Storage for the starter's 50 articles, logical bytes: full copies vs cleaned prose and this design",
            ["Part", "MiB", "Share of full copies"],
            [["Full copy of every version's wikitext (dataset M; the starter's 2,049 MB is decimal: 1,954 MiB)", round(mb(full[0]), 1), 1.0],
             ["Cleaned paragraph text, full copy per visible revision", round(mb(clean), 1), round(clean / full[0], 4)],
             ["claim rows (ids, hashes, section, editor, comment)", round(mb(claim_b), 1), round(claim_b / full[0], 4)],
             ["layout rows (reading order)", round(mb(layout_b), 1), round(layout_b / full[0], 4)],
             ["chunk.text (one row per normalized text)", round(mb(text_b), 1), round(text_b / full[0], 4)],
             ["text_variant.text (exact spellings; text is stored in both tables)", round(mb(var_b), 1), round(var_b / full[0], 4)],
             ["text total", round(mb(text_total), 1), round(text_total / full[0], 4)],
             ["vectors (4 KB per distinct text)", round(mb(n_vec * 4096), 1), round(n_vec * 4096 / full[0], 4)],
             [f"FULLTEXT index ({len(fts_files)} physical files)", round(fts_mb, 1), round(fts_mb * 2**20 / full[0], 4)]])
    C.add("F6.5", "Storage: claims + layout + chunk text (+ vectors) against full copies of every version, same 50 articles",
          "oracle", "recorded; plan est. one to two orders of magnitude below full copies (2,049 MB wikitext, 1,202 MB compressed [S2]), "
          "separate markup removal from deduplication; count chunk.text and text_variant.text as two stored copies",
          f"raw wikitext {mb(full[0]):,.0f} MiB over {full[1]:,} versions; cleaned prose {mb(clean):,.1f} MiB over {clean_versions:,} visible revisions "
          f"({full[0] / clean:.1f}x smaller after markup/boilerplate removal); distinct paragraph strings {mb(stored_text):,.1f} MiB, "
          f"including both chunk.text and text_variant.text ({clean / stored_text:.1f}x smaller than cleaned full copies); claim/layout metadata "
          f"{mb(claim_b + layout_b):,.1f} MiB; total text {mb(text_total):,.1f} MiB ({full[0] / text_total:.1f}x below raw wikitext); "
          f"vectors {mb(n_vec * 4096):.1f} MiB. On disk for all 60 pages: claim {files['claim']} MiB, layout {files['layout']} MiB, "
          f"text_variant {files['text_variant']} MiB, chunk {files['chunk']} MiB, vector index {vec_files:.0f} MiB, "
          f"FULLTEXT index {len(fts_files)} files / {fts_mb:.1f} MiB", None)


# ---------- F6.6 latency ----------
def check_latency(revs: dict) -> None:
    cur = connect("wikihistory").cursor()
    cur.execute("SHOW INDEX FROM claim WHERE Column_name = 'row_start' AND Seq_in_index = 2"); key = cur.fetchone()[2]
    rnd = random.Random(67); pages = sorted(revs); big = sorted(revs, key=lambda p: -len(revs[p]))[:3]
    picks = []
    for i in range(1500):
        p = big[i % 3] if i >= 1000 else rnd.choice(pages)
        lo, hi = revs[p][0][3], revs[p][-1][3]
        old = i % 2 == 0
        span = (hi - lo) * (0.2 if old else 1.0)
        picks.append((p, lo + timedelta(microseconds=rnd.randrange(max(1, int(span.total_seconds() * 1e6)))), old, i >= 1000))
    rows, key_counts = [], {}
    variants = (("KEY(page_id, row_start) forced", f"FORCE INDEX ({key})"), ("optimizer's choice", ""),
                ("without KEY(page_id, row_start)", f"IGNORE INDEX ({key})"))
    for name, hint in variants:
        ms: dict[str, list[float]] = {"all": [], "largest 3 pages": [], "old dates (first 20% of a page's life)": []}
        key_counts[name] = {part: {"layout": Counter(), "claim": Counter()} for part in ms}
        for p, t, old, is_big in picks:
            s = L.ts_text(t)
            cur.execute("EXPLAIN SELECT claim_order FROM layout FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?", (s, p))
            layout_cols = [c[0] for c in cur.description]
            layout_key = cur.fetchall()[0][layout_cols.index("key")]
            cur.execute(f"EXPLAIN SELECT claim_id, text_hash, exact_hash FROM claim FOR SYSTEM_TIME AS OF TIMESTAMP ? {hint} WHERE page_id = ?", (s, p))
            claim_cols = [c[0] for c in cur.description]
            claim_key = cur.fetchall()[0][claim_cols.index("key")]
            part = "largest 3 pages" if is_big else "all"
            key_counts[name][part]["layout"][layout_key or "(none)"] += 1
            key_counts[name][part]["claim"][claim_key or "(none)"] += 1
            if old:
                key_counts[name]["old dates (first 20% of a page's life)"]["layout"][layout_key or "(none)"] += 1
                key_counts[name]["old dates (first 20% of a page's life)"]["claim"][claim_key or "(none)"] += 1
            t0 = time.perf_counter()
            cur.execute("SELECT claim_order FROM layout FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?", (s, p)); cur.fetchall()
            cur.execute(f"SELECT claim_id, text_hash, exact_hash FROM claim FOR SYSTEM_TIME AS OF TIMESTAMP ? {hint} WHERE page_id = ?", (s, p)); cur.fetchall()
            dt = 1000 * (time.perf_counter() - t0)
            (ms["largest 3 pages"] if is_big else ms["all"]).append(dt)
            if old:
                ms["old dates (first 20% of a page's life)"].append(dt)
        for part, v in ms.items():
            q = statistics.quantiles(v, n=100)
            def fmt_keys(which: str) -> str:
                return ", ".join(f"{k} {n}/{len(v)}" for k, n in sorted(key_counts[name][part][which].items()))
            rows.append([name, part, len(v), fmt_keys("layout"), fmt_keys("claim"), round(statistics.median(v), 2), round(q[94], 2), round(max(v), 2)])
    C.table("AS OF reconstruction latency and EXPLAIN key distribution for every timed query, ms", ["Variant", "Queries", "n",
            "Layout keys", "Claim keys", "p50", "p95", "max"], rows)
    optimizer = [r for r in rows if r[0] == "optimizer's choice"]
    opt_all = next(r for r in optimizer if r[1] == "all")
    opt_worst = max(r[6] for r in optimizer)
    C.add("F6.6", "Latency: AS OF for one page (both queries), with and without KEY(page_id, row_start), on random, largest and old pages",
          "smoke", "optimizer's actual plan has p95 <= 10 ms for the two queries reconstruct() runs; forced-key and no-key timings are recorded",
          f"optimizer: p95 {opt_all[6]} ms on random pages, worst group p95 {opt_worst} ms; EXPLAIN keys recorded per timed query in the table; "
          f"forced-key and no-key results are also shown",
          opt_worst <= 10)


# ---------- F6.7 fault injection ----------
def check_mutations() -> None:
    path = EV / "f6_mutations.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"results": []}
    sha = lambda f: hashlib.sha256(f.read_bytes()).hexdigest()
    fresh = (doc.get("code") == {n: sha(ROOT / n) for n in L.MUTATION_CODE_FILES}
             and doc.get("live_checksum") == L.checksum("wikihistory"))
    r = doc["results"]
    C.table("Fault injection (eval/f6_mutate.py, on a copy in smoke): targeted sub-checks", ["Fault", "Expected failing sub-checks",
            "Observed failing sub-checks"], [[x["fault"], ", ".join(x["expected_failed"]) or "none", ", ".join(x["failed"]) or "none"] for x in r])
    C.add("F6.7", "Fault injection: round trip, set equality, SQL integrity and independent rebuild respond to targeted faults",
          "invariant", "each fault is caught by its specified sub-checks; evidence is bound to loader, checker, library and loaded tables",
          f"{sum(x['ok'] for x in r)} of {len(r)} as expected; fresh {fresh}", bool(r) and all(x["ok"] for x in r) and fresh)


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sys.path.insert(0, str(ROOT / "pipeline"))
    s: dict = {}
    steps = [check_provenance, lambda: s.update(revs=L.revisions()), lambda: s.update(exp=L.expected_lists(s["revs"])),
             lambda: check_round_trip(s["revs"], s["exp"]), lambda: check_integrity(s["revs"]), check_idempotence, check_storage,
             lambda: check_latency(s["revs"]), check_mutations]
    for i, step in enumerate(steps):
        t = time.perf_counter()
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"F6.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
        print(f"  step {i}: {time.perf_counter() - t:.1f} s", flush=True)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
