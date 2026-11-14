"""F5.6: how the golden-probe targets were chosen, as code. Re-creates both target files exactly; F5.6 checks they match.

    .venv/bin/python eval/f5_probe_targets.py          # prints whether evidence/f5_probe_targets*.json follow these rules

Rule (both sets): a target is a paragraph alive in a page's latest non-empty revision (character-only lineage), 350 to
1,100 characters, with no list marker '*'; one target per page, pages and paragraphs drawn with a fixed seed.
  original  seed 2031: 10 pages, languages fi x3, sv x2, en x3, fr x2 in that order;
  fresh     seed 2032: 40 other pages' paragraphs (not an original target), 10 per language in shuffled order.
A model agent then wrote one question per target (frozen in f5_probes*.json before any retrieval). Every target is a
current paragraph, so no probe asks about a past date: time travel is F7's job (Step 6).
"""
import json, random
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CH, LIN, EV = ROOT / "data/chunks", ROOT / "data/lineage", ROOT / "docs/validation/evidence"


def candidates() -> tuple[list[tuple], dict[int, str]]:
    d = duckdb.connect()
    titles = dict(d.execute(f"SELECT page_id::INT, title FROM read_csv('{ROOT}/docs/hero.tsv', delim='\t', header=true)").fetchall())
    rows = d.execute(f"""WITH last AS (SELECT page_id lp, max(ord) o FROM '{CH}/revisions.parquet' WHERE NOT hidden AND n_chunks > 0 GROUP BY 1)
        SELECT i.page_id, i.claim_id, r.section, c.text FROM '{LIN}/claim_ids.parquet' i JOIN last l ON l.lp = i.page_id AND l.o = i.ord
        JOIN '{CH}/rev_chunks.parquet' r ON r.page_id = i.page_id AND r.ord = i.ord AND r.pos = i.pos
        JOIN '{CH}/chunk_text.parquet' c ON c.text_hash = r.text_hash
        WHERE length(c.text) BETWEEN 350 AND 1100 AND c.text NOT LIKE '%*%' ORDER BY 1, 2""").fetchall()
    return rows, titles


def draw(rows: list[tuple], titles: dict[int, str], seed: int, langs: list[str], shuffle: bool, exclude: set) -> list[dict]:
    rnd = random.Random(seed)
    pool = [r for r in rows if (r[0], r[1]) not in exclude]
    pages = rnd.sample(sorted({r[0] for r in pool}), len(langs))
    if shuffle:
        rnd.shuffle(langs)
    out = []
    for k, (p, lang) in enumerate(zip(pages, langs, strict=True), 1):
        _, cid, sec, text = rnd.choice([r for r in pool if r[0] == p])
        out.append(dict(probe=k, lang=lang, page=titles[p], page_id=p, claim_id=cid, section=sec or "(lead)", paragraph=text))
    return out


def targets() -> dict[str, list[dict]]:
    rows, titles = candidates()
    orig = draw(rows, titles, 2031, ["fi", "fi", "fi", "sv", "sv", "en", "en", "en", "fr", "fr"], False, set())
    fresh = draw(rows, titles, 2032, [lang for lang in ("fi", "sv", "en", "fr") for _ in range(10)], True,
                 {(t["page_id"], t["claim_id"]) for t in orig})
    return {"f5_probe_targets.json": orig, "f5_probe_targets_fresh.json": fresh}


def follows_rules() -> dict[str, bool]:
    return {name: json.loads((EV / name).read_text(encoding="utf-8")) == want for name, want in targets().items()}


if __name__ == "__main__":
    print(follows_rules())
