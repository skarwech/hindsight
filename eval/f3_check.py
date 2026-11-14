"""F3 — chunker and hashing (plan section 7, F3; gate H8). Rebuilt after the adversarial review of 2026-11-14.

    .venv/bin/python eval/f3_check.py            # writes docs/validation/f3.json (~3 min)
"""
import csv, json, random, re, statistics, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import duckdb
import xxhash

from common import ROOT, Checks, connect
import f3_oracle as oracle

sys.path.insert(0, str(ROOT / "pipeline"))
from chunker import BOILERPLATE, OTHER, REF, SECTION_MAX, chunk_revision, clean_blocks, norm, paragraphs, text_hash  # noqa: E402

C = Checks("F3", "Chunker and hashing", "H8")
CH = ROOT / "data/chunks"
M_GLOB = str(ROOT / "data/m/versions-*.parquet")
D = duckdb.connect()
P = {name: str(CH / f"{name}.parquet") for name in ("rev_chunks", "chunk_text", "chunk_variant", "revisions")}


def hero() -> dict[int, str]:
    with open(ROOT / "docs/hero.tsv", encoding="utf-8") as f:
        return {int(r["page_id"]): r["title"] for r in csv.DictReader(f, delimiter="\t")}


def raw_texts(revs: list[int]) -> dict[int, str]:
    rows = D.execute(f"SELECT rev_id, text FROM read_parquet(?) WHERE rev_id IN ({','.join(map(str, revs))})", [M_GLOB]).fetchall()
    return {r: t or "" for r, t in rows}


def sample_revisions(n: int, seed: int) -> list[int]:
    revs = [r for r, h in D.execute("SELECT rev_id, hidden FROM read_parquet(?)", [P["revisions"]]).fetchall() if not h]
    return random.Random(seed).sample(sorted(revs), n)


# ---------- F3.1 determinism ----------
def check_determinism(pages: dict[int, str]) -> None:
    pick = sorted(random.Random(3).sample(sorted(set(pages) - {1256}), 4)) + [1256]       # 4 random pages + Talvisota
    revs = D.execute(f"SELECT rev_id FROM read_parquet(?) WHERE page_id IN ({','.join(map(str, pick))})", [P["revisions"]]).fetchall()
    text = raw_texts([r for (r,) in revs])
    want = defaultdict(list)
    for rev, sec, th, eh, nr in D.execute(f"SELECT rev_id, section, text_hash, exact_hash, n_refs FROM read_parquet(?) "
                                         f"WHERE page_id IN ({','.join(map(str, pick))}) ORDER BY page_id, ord, pos", [P["rev_chunks"]]).fetchall():
        want[rev].append((sec, th, eh, nr))
    bad = sum([(c.section, c.text_hash, xxhash.xxh64_intdigest(c.text.encode()), c.n_refs) for c in chunk_revision(text[r])] != want.get(r, [])
              for (r,) in revs)
    C.add("F3.1", "Determinism: a serial re-run equals the stored parallel run (section, hash, exact spelling, citations)", "invariant",
          f"0 of {len(revs):,} revisions differ (5 pages)", f"{bad} of {len(revs):,} differ ({', '.join(pages[p] for p in pick)})", bad == 0)


# ---------- F3.2 boundary stability, whole population ----------
def check_stability() -> None:
    rows = D.execute("SELECT page_id, ord, rev_id, hidden FROM read_parquet(?) ORDER BY page_id, ord", [P["revisions"]]).fetchall()
    pairs = [(a[2], b[2]) for a, b in zip(rows, rows[1:]) if a[0] == b[0] and not a[3] and not b[3]]
    by_rev = defaultdict(list)
    for rev, h in D.execute("SELECT rev_id, text_hash FROM read_parquet(?)", [P["rev_chunks"]]).fetchall():
        by_rev[rev].append(h)
    added, changed = [], []
    for a, b in pairs:
        ca, cb = Counter(by_rev[a]), Counter(by_rev[b])
        plus, minus = sum((cb - ca).values()), sum((ca - cb).values())
        added.append(plus); changed.append(max(plus, minus))         # an in-place edit changes 1; an insert adds 1
    changed.sort()
    q = lambda p: changed[min(len(changed) - 1, int(p * len(changed)))]
    C.add("F3.2", f"Boundary stability: chunk hashes changed per edit, all {len(pairs):,} consecutive revision pairs", "invariant",
          "median <= 2 and P90 <= 6 (changed = max(added, removed) per pair)",
          f"median {statistics.median(changed):g}, P90 {q(0.9)}, P99 {q(0.99)}, max {changed[-1]}; {sum(c == 0 for c in changed) / len(changed):.0%} "
          f"of edits change no chunk; mean added {statistics.mean(added):.2f}", statistics.median(changed) <= 2 and q(0.9) <= 6)
    bins = [("0 (no chunk text changed)", 0, 0), ("1", 1, 1), ("2", 2, 2), ("3–6", 3, 6), ("7–20", 7, 20), ("> 20 (blanking, mass rewrite)", 21, 10 ** 9)]
    C.table(f"Chunk hashes changed per edit (all {len(pairs):,} consecutive revision pairs of the hero pages)", ["Changed", "Edits"],
            [[label, sum(lo <= c <= hi for c in changed)] for label, lo, hi in bins])


# ---------- F3.3 coverage against the starter's cleaner ----------
def check_coverage() -> None:
    revs = sample_revisions(50, 11); text = raw_texts(revs)
    hit = total = lost_content = boiler = 0
    for rev in revs:
        blocks, mine = oracle.clean(text[rev]), clean_blocks(text[rev])
        levels = [lvl for k, lvl, _ in mine if k == "h"]
        chunks = [c.text for c in chunk_revision(text[rev])]
        h, t = oracle.covered(blocks, chunks); hit += h; total += t
        joined, skip, hi = norm(" ".join(chunks)), None, 0             # independent walk over the starter's blocks
        for k, txt in blocks:
            if k == "h":
                lvl = levels[hi] if hi < len(levels) else 2; hi += 1
                if skip is None or lvl <= skip:
                    skip = lvl if txt.rstrip(":").strip().casefold() in BOILERPLATE else None
                continue
            n = norm(txt)
            if skip is not None:
                boiler += len(n)
            elif n and n not in joined:
                lost_content += len(n)
    C.add("F3.3", "Coverage: chunk text covers the starter-cleaned article (independent oracle, 50 revisions)", "invariant",
          ">= 95% of cleaned characters (plan); content lost outside boilerplate sections <= 0.5%",
          f"{hit / total:.2%} covered; {boiler / total:.1%} is boilerplate sections; content lost elsewhere {lost_content / total:.3%}",
          hit / total >= 0.95 and lost_content / total <= 0.005)


# ---------- F3.4 leaks, F3.6 sizes ----------
def check_leaks_and_sizes() -> None:
    rows = D.execute("SELECT text_hash, text, chars, bytes FROM read_parquet(?)", [P["chunk_text"]]).fetchall()
    variants = D.execute("SELECT text FROM read_parquet(?)", [P["chunk_variant"]]).fetchall()
    n = len(rows)
    marks = ("{{", "[[", "<ref", "{|")
    leaks = [(h, t) for h, t, _, _ in rows if any(m in t for m in marks)]
    leaky_variants = sum(any(m in t for m in marks) for (t,) in variants)
    entities = sum(bool(re.search(r"&(?:[a-zA-Z]{2,8}|#\d{2,5});", t)) for _, t, _, _ in rows)
    first = dict(D.execute("SELECT text_hash, MIN(rev_id) FROM read_parquet(?) GROUP BY 1", [P["rev_chunks"]]).fetchall())
    C.add("F3.4", "Markup leaks ({{, [[, <ref, {|) and HTML entities in distinct chunks", "invariant",
          "<= 0.5% of chunks; every hit listed; entities decoded",
          f"{len(leaks)} of {n:,} ({len(leaks) / n:.2%}); in any spelling {leaky_variants:,} of {len(variants):,}; {entities} with an entity",
          len(leaks) / n <= 0.005 and leaky_variants / len(variants) <= 0.005,
          "\n".join(f"rev {first[h]}: …{t[max(0, i - 60):i + 60]!r}…" for h, t in leaks
                    for i in [min(t.find(m) for m in marks if m in t)]))
    in_band = sum(60 <= c <= 1500 for _, _, c, _ in rows) / n
    empty = sum(not t.strip() for _, t, _, _ in rows)
    placeholders = sum(REF in t or OTHER in t for _, t, _, _ in rows) + sum(REF in t or OTHER in t for (t,) in variants)
    fffd = [h for h, t, _, _ in rows if "�" in t]               # U+FFFD already in the saved wikitext is the source's
    src = raw_texts([first[h] for h in fffd]) if fffd else {}
    introduced = sum("�" not in src.get(first[h], "") for h in fffd)
    long_sections = D.execute("SELECT COUNT(*) FROM read_parquet(?) WHERE length(section) > ?", [P["rev_chunks"], SECTION_MAX]).fetchall()[0][0]
    max_chars, max_bytes = max(c for _, _, c, _ in rows), max(b for _, _, _, b in rows)
    C.add("F3.6", "Sizes and encoding: length band, empties, MariaDB TEXT and VARCHAR(255) bounds, UTF-8, placeholders", "invariant",
          ">= 90% in 60–1,500 chars; 0 empty; < 65,535 bytes; sections <= 255 chars; 0 U+FFFD introduced; 0 placeholders",
          f"{in_band:.1%} in band; {empty} empty; max {max_chars:,} chars / {max_bytes:,} bytes; {long_sections} long sections; "
          f"{introduced} U+FFFD introduced ({len(fffd)} in the source); {placeholders} placeholders",
          in_band >= 0.9 and empty == 0 and max_bytes < 65535 and long_sections == 0 and introduced == 0 and placeholders == 0)


# ---------- F3.5 citation counts ----------
def _align(items: list, chunks: list) -> list[tuple[int, int, str]]:
    """(expected refs, n_refs, kind) for every chunk that equals a run of consecutive raw lines."""
    out, start = [], 0
    repeats = Counter(norm(c.text) for c in chunks)
    for c in chunks:
        target = norm(c.text)
        if repeats[target] > 1:                                    # identical chunks in one revision: alignment is ambiguous
            continue
        for i in range(start, len(items)):
            it = items[i]
            if not it or it[2] not in ("p", "li") or not it[0] or not target.startswith(it[0]):
                continue
            acc, refs, j, lines = it[0], it[1], i + 1, 1
            while acc != target and j < len(items) and items[j] and items[j][2] != "h" and len(acc) < len(target):
                if items[j][0]:
                    acc, lines = acc + " " + items[j][0], lines + 1
                refs += items[j][1]; j += 1
            if acc != target:
                continue
            while j < len(items) and items[j] and items[j][2] != "h" and not items[j][0]:   # wordless lines: their citations back the paragraph before
                refs += items[j][1]; j += 1
            out.append((refs, c.n_refs, "list" if "\n" in c.text and it[2] == "li" else "merged" if lines > 1 else "single"))
            start = j
            break
    return out


def check_refs() -> None:
    compared, by_kind, bad = [], Counter(), []
    sample = sample_revisions(120, 5); texts = raw_texts(sample)
    open_ref, close_ref = re.compile(r"<ref(?!erences)[^>]*(?<!/)>", re.I), re.compile(r"</ref\s*>", re.I)
    for rev in sample:
        t = texts[rev]
        items = []
        for line in re.sub(r"<!--.*?-->", "", t, flags=re.S).split("\n"):
            if (line.count("{{") != line.count("}}") or line.count("{|") != line.count("|}")
                    or len(open_ref.findall(line)) != len(close_ref.findall(line))):
                items.append(None); continue                       # a construct spanning lines: unknown, breaks a run
            b = oracle.clean(line)
            refs = oracle.line_refs(line)
            items.append(("", refs, "empty") if not b else (norm(b[0][1]), refs, b[0][0]) if len(b) == 1 else None)
        for exp, got, kind in _align(items, chunk_revision(t)):
            compared.append(exp == got); by_kind[kind] += 1
            if exp != got:
                bad.append(f"rev {rev} ({kind}): raw {exp}, n_refs {got}")
    inv_bad = 0
    inv = sample_revisions(300, 6); inv_texts = raw_texts(inv)
    for rev in inv:
        t = inv_texts[rev]; stats = Counter()
        kept = sum(p.count(REF) for _, _, p in paragraphs(clean_blocks(t)))
        inv_bad += sum(c.n_refs for c in chunk_revision(t, stats)) + stats["orphan_refs"] != kept
    C.add("F3.5", "n_refs equals the citations on the raw lines each chunk came from (oracle cleaner, all chunk kinds)", "golden",
          "every aligned chunk agrees; >= 1,000 compared incl. merged and list chunks; per-revision sum invariant holds",
          f"{sum(compared):,} of {len(compared):,} agree ({dict(by_kind)}); invariant broken in {inv_bad} of 300 revisions",
          all(compared) and len(compared) >= 1000 and by_kind["merged"] >= 20 and by_kind["list"] >= 20 and inv_bad == 0,
          "\n".join(bad[:40]) or "all agree")


# ---------- F3.7 the starter's cleaner on every distinct text ----------
def _compare_page(page_id: int) -> tuple[int, list[int]]:
    """Every distinct text of one page (one Parquet read per page): (texts compared, rev_ids that differ)."""
    rows = duckdb.connect().execute("SELECT MIN(rev_id), any_value(text) FROM read_parquet(?) WHERE page_id = ? "
                                    "GROUP BY COALESCE(sha1, rev_id::VARCHAR)", [M_GLOB, page_id]).fetchall()
    bad = [r for r, t in rows if [(k, x.replace(REF, "").strip()) for k, _, x in clean_blocks(t or "")] != oracle.clean(t or "")]
    return len(rows), bad


def check_starter_cleaner(pages: dict[int, str]) -> None:
    with ProcessPoolExecutor(8) as pool:
        results = list(pool.map(_compare_page, sorted(pages)))
    n, bad = sum(r[0] for r in results), [b for r in results for b in r[1]]
    C.add("F3.7", "Cleaner = the starter's history.clean() with the five documented fixes, on every distinct hero text", "oracle",
          f"{n:,} of {n:,} identical", f"{n - len(bad):,} of {n:,} identical", not bad and n > 30000, bad[:20])


# ---------- F3.8 output integrity, F3.9 N ----------
def check_integrity(pages: dict[int, str]) -> None:
    cur = connect("wikihistory").cursor()
    cur.execute(f"SELECT page_id, rev_id FROM page_state FOR SYSTEM_TIME ALL WHERE page_id IN ({','.join(map(str, pages))}) ORDER BY page_id, row_start, rev_id")
    want = cur.fetchall()
    got = D.execute("SELECT page_id, rev_id FROM read_parquet(?) ORDER BY page_id, ord", [P["revisions"]]).fetchall()
    gaps = D.execute("""SELECT COUNT(*) FROM (SELECT rev_id, COUNT(*) n, MIN(pos) lo, MAX(pos) hi, COUNT(DISTINCT pos) d FROM read_parquet(?) GROUP BY rev_id) x
                        JOIN read_parquet(?) r USING (rev_id) WHERE lo <> 0 OR hi <> n - 1 OR d <> n OR n <> r.n_chunks""",
                     [P["rev_chunks"], P["revisions"]]).fetchall()[0][0]
    texts = D.execute("SELECT text_hash, text FROM read_parquet(?)", [P["chunk_text"]]).fetchall()
    variants = D.execute("SELECT exact_hash, text_hash, text FROM read_parquet(?)", [P["chunk_variant"]]).fetchall()
    bad_hash = sum(text_hash(t) != h for h, t in texts) + sum(text_hash(t) != h or xxhash.xxh64_intdigest(t.encode()) != e for e, h, t in variants)
    orphans = D.execute("""SELECT (SELECT COUNT(*) FROM read_parquet(?) r ANTI JOIN read_parquet(?) v USING (exact_hash, text_hash)),
                                  (SELECT COUNT(*) FROM read_parquet(?) r ANTI JOIN read_parquet(?) t USING (text_hash))""",
                        [P["rev_chunks"], P["chunk_variant"], P["rev_chunks"], P["chunk_text"]]).fetchall()[0]
    C.add("F3.8", "Output integrity: revision set and order = page_state; positions contiguous; every hash recomputes; no dangling hash",
          "invariant", "all equal; 0 gaps; 0 bad hashes; 0 dangling",
          f"revisions {'equal' if want == got else 'DIFFER'} ({len(got):,}); {gaps} position gaps; {bad_hash} bad hashes "
          f"over {len(texts):,} texts + {len(variants):,} spellings; dangling {orphans[0]} spellings, {orphans[1]} texts",
          want == got and gaps == 0 and bad_hash == 0 and orphans == (0, 0))


def check_n(pages: dict[int, str]) -> None:
    log = json.loads((ROOT / "logs/chunk_hero.json").read_text(encoding="utf-8"))
    n, rows = D.execute("SELECT (SELECT COUNT(*) FROM read_parquet(?)), (SELECT COUNT(*) FROM read_parquet(?))", [P["chunk_text"], P["rev_chunks"]]).fetchall()[0]
    merged = D.execute("SELECT COUNT(*) FILTER (WHERE spellings > 1) FROM read_parquet(?)", [P["chunk_text"]]).fetchall()[0][0]
    rep = dict(D.execute("SELECT text_hash, text FROM read_parquet(?)", [P["chunk_text"]]).fetchall())
    rep_exact = {h: xxhash.xxh64_intdigest(t.encode()) for h, t in rep.items()}
    differs = sum(rep_exact[h] != e for h, e in D.execute("SELECT text_hash, exact_hash FROM read_parquet(?)", [P["rev_chunks"]]).fetchall())
    empty = D.execute("SELECT COUNT(*) FILTER (WHERE n_chunks = 0 AND hidden), COUNT(*) FILTER (WHERE hidden), COUNT(*) FILTER (WHERE n_chunks = 0 AND NOT hidden) FROM read_parquet(?)",
                      [P["revisions"]]).fetchall()[0]
    C.add("F3.9", "N, the distinct chunk hashes (sizes Step 4), and what the run recorded", "smoke",
          "plan est.: 10^4–10^5 for 50 pages; warning sign far above 10^5",
          f"N = {n:,} from {rows:,} (revision, chunk) rows over {len(pages)} pages, {log['seconds']} s; embedding at ~32/s ≈ {n / 32 / 60:.0f} min; "
          f"{merged:,} hashes join several spellings ({differs / rows:.1%} of rows use a non-representative spelling, kept in chunk_variant); "
          f"placeholder fallbacks {log['placeholder_fallbacks']}; orphan citations {log['orphan_refs']:,}; "
          f"hidden revisions with 0 chunks {empty[0]} of {empty[1]}; other revisions with 0 chunks {empty[2]} (blanked or emptied)", None)
    per = D.execute("""SELECT page_id, COUNT(DISTINCT rev_id), COUNT(DISTINCT text_hash), ROUND(COUNT(*) / COUNT(DISTINCT rev_id), 1)
                       FROM read_parquet(?) GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", [P["rev_chunks"]]).fetchall()
    C.table("Largest hero pages: revisions, distinct chunks ever seen, chunks per revision",
            ["Page", "Revisions", "Distinct chunks", "Chunks / revision"], [[pages[p], r, k, float(c)] for p, r, k, c in per])
    sample = D.execute("""SELECT r.pos, r.section, length(v.text), r.n_refs, left(v.text, 140) FROM read_parquet(?) r JOIN read_parquet(?) v USING (exact_hash)
                          WHERE r.page_id = 1256 AND r.ord = (SELECT MAX(ord) FROM read_parquet(?) WHERE page_id = 1256) ORDER BY r.pos LIMIT 8""",
                       [P["rev_chunks"], P["chunk_variant"], P["revisions"]]).fetchall()
    C.table("Talvisota at dataset end: the first 8 chunks", ["Pos", "Section", "Chars", "Refs", "Text"],
            [[str(p), s or "(lead)", ch, nr, t + "…"] for p, s, ch, nr, t in sample])


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pages = hero()
    steps = [lambda: check_determinism(pages), check_stability, check_coverage, check_leaks_and_sizes, check_refs,
             lambda: check_starter_cleaner(pages), lambda: check_integrity(pages), lambda: check_n(pages)]
    for i, step in enumerate(steps):
        t = time.perf_counter()
        try:
            step()
        except Exception as e:                    # a crashing check is a red line, not a missing one
            C.add(f"F3.err{i}", f"check step {i} crashed", "invariant", "runs", repr(e)[:500], False)
        print(f"  step {i}: {time.perf_counter() - t:.1f} s", flush=True)
    print("wrote", C.save(stamp))
    sys.exit(0 if all(c["status"] != "fail" for c in C.doc["checks"]) else 1)


if __name__ == "__main__":
    main()
