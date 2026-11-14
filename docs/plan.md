# Hindsight: ask Wikipedia what it knew, when it knew it

Junction 2026 · MariaDB challenge "Build the tools Wikipedia is missing" (RAG track plus system-versioned-table track)

Complete plan: concept, architecture, step-by-step execution, feature specifications with validation conditions, and the code that was tested.

- Version: 13 November 2026.
- Status: plan, plus building blocks that were executed in a sandbox (section 13). Vector search, embeddings, LLM calls and numbers on the real data were not executed. Every figure marked "est." is an estimate to be replaced by a measurement.
- References in square brackets, for example [S2], point to the source list at the end.

## Contents

1. TL;DR
2. The challenge in one page
3. What the starter repos change
4. The product
5. Architecture and data model
6. Execution, step by step
7. Feature specs and validation (F1 to F17)
8. Gates, cut lines, team shapes
9. What the starters left open, and the number on the slide
10. Demo script
11. Risks, pitfalls, mentor questions
12. Validation dashboard
13. What was verified in a sandbox
- Appendix A: resolver unit tests and simulation
- Appendix B: toy end-to-end harness (load and round trip)
- Appendix C: pitfalls.sql (re-run it on the real server)
- Sources

---

## 1. TL;DR

- Product: Hindsight answers questions about Finnish history from Wikipedia as it stood on any date, shows which claims were fought over, and checks any AI answer against the record of that date. A claim is a paragraph-level unit tracked across rewrites, moves, deletions and revivals.
- Tracks: BOTH. The starters already ship an as-of reader with a revert timeline [S2] and article-level semantic search [S3]. The win is the part they leave open: chunk-level, time-aware RAG over claim-level history, plus a contested-ness layer that shows what was fought over, not only when.
- Stack: one MariaDB instance (`mariadb:lts`; vectors need 11.7 or later [S3]) with two schemas; bge-m3 embeddings, the model of dataset S [S1]; FastAPI; a web app (React, or a fork of the SVT starter's server-rendered page); an optional Wikipedia userscript.
- Core design: claims are rows and rewrites are history rows. Text and vectors are stored once per distinct paragraph in a content-addressed table. Retrieval routes with vectors, then ranks exactly on the claims alive on the chosen date. A metadata-only system-versioned table covers all 862,076 versions and drives triage.
- Plan shape: 13 steps (0 to 12), 17 features (F1 to F17), three gates (H14, H24, H32), feature freeze at H36, submission at H42. Hours count from the start of hacking.
- Evidence standard: every feature has invariants, oracles and golden sets with explicit pass conditions (section 7) and a dashboard (section 12). A feature is demoed only while its line is green.
- Verified so far: the lineage resolver and the system-versioning data model were executed on MariaDB 10.11 (round trip 1,242 of 1,242 on a synthetic history). See section 13.

## 2. The challenge in one page

Mission. Build a tool for Wikipedia that does not exist yet, or exists but is badly underused, with MariaDB at the core of the stack.

Tracks.
- RAG track: MariaDB native vector search for retrieval-augmented generation, giving grounded, source-cited answers (for example a research assistant for the history of Finland).
- SVT track: MariaDB system-versioned tables for tools that query how knowledge changes over time: what an article said on a given date, edit wars, how the story of an event was rewritten.
- BOTH: for example a RAG assistant that answers "what did Wikipedia say about this in 2015, and what has changed since?"

Judging. Effective and creative use of MariaDB RAG and/or system-versioned tables; technical execution and scalability across the provided datasets; real-world usefulness for Wikipedia readers or editors; design and demo quality. Teams on the SVT track, or combining both, are recognised for added technical ambition.

Prizes. 1st: EUR 1,000 cash, EUR 1,000 UpCloud credits and MariaDB certification vouchers for the team. 2nd: EUR 500. 3rd: EUR 250. Winners are credited in a mariadb.org blog post.

Hints in the brief.
- LLMs have a worldview from their training data that may differ from Wikipedia's neutral point of view (NPOV).
- Two audiences: readers and active editors. Either or both.
- Wanted: source-grounded Q&A, fact-checking assistants, detection of gaps and weak spots in coverage, cross-language comparison.
- On the SVT side: point-in-time views, change visualisations, edit wars or bias detection, edit-history analytics.
- Source articles are in Finnish, so a multilingual embedding model is recommended. No Finnish skills are needed. Any LLM, embedding model or frontend is allowed.

Resources. Three datasets (S, M, L), three starter repos, and mentors Kaj Arnö (MariaDB Foundation Executive Chairman) and Robert Silén (MariaDB Foundation AI Lead) [S10].

Event. Junction 2026 is listed for 13 to 15 November at Hype Arena in Espoo, with teams of 1 to 4 [S9]. Plan for 3 to 4 people and about 40 usable hours.

Decision. BOTH tracks, because (a) the scoring rewards ambition, (b) the SVT starter already covers a plain as-of reader, (c) the RAG starter has no Ask step or chunks, and (d) the intersection is the example the brief itself names for very advanced teams.

## 3. What the starter repos change

1. The datasets are different data, not three sizes. S holds the current text of 19,285 Finnish, 4,921 Swedish and 5,254 English history articles with one bge-m3 vector each. M holds all 862,076 saved versions (2002 to 1 October 2026) of 19,742 Finnish articles as raw wikitext, with editor, anonymous flag, edit summary and sha1. L holds 568,416 current Finnish articles with no vectors [S1]. So BOTH means S vectors for routing plus M for history, and L is a scale test you must embed yourself. Every row keeps Wikipedia's own `page_id` and `rev_id` [S1], so the tables join directly and every result can link to `?oldid=<rev_id>`.

2. The baseline already exists. The SVT starter ships an as-of reader, a monthly timeline with reverts split into anonymous cleanup and named-user reverts, edit-war shading and a two-moment diff. It lists as open: the RAG combination, a Parquet loader, loading all ~19,750 articles, and the cost of keeping a full copy per version [S2]. The RAG starter embeds whole articles only, has no Ask step, chunks or hybrid search yet, and misses facts buried in long biographies [S3]. Hindsight is the intersection of those open items.

3. Backfill with `system_versioning_insert_history`, not `SET timestamp` (MariaDB 10.11 or later). Earlier versions are inserted with both `row_start` and `row_end`, the current one with a start only; same-second edits are nudged by a microsecond; sessions run in UTC; batches are capped by size [S2]. Use `mariadb:lts` (the starters ran 12.3.x). Vector search needs 11.7 or later [S3].

4. The index rules shape the schema. A table can have one vector index, vectors must be NOT NULL, and partitioning is unsupported [S4]. The SVT starter partitions its table by year, so an embedding column cannot simply be added to it. MariaDB's tracker notes that duplicate vectors arise easily in system-versioned tables [S5], and one report shows a LIMIT 30 vector search on a versioned table returning 19 rows after deletes and updates, closed as not a bug [S6]. In that report the vector column was declared `WITHOUT SYSTEM VERSIONING`, so it is not proof for your exact setup; smoke test T3 exists to settle it. My working hypothesis: history rows sit in the HNSW graph and the time filter is applied afterwards. Hence vectors live once per distinct text in a plain table, and time travel is a join.

5. "Git blame for Wikipedia" exists. WikiWho attributes authorship per token [S8] and powers Who Wrote That?, XTools Authorship and XTools Blame; Finnish Wikipedia is supported [S7]. Do not pitch blame. The differentiator is claims over time, plus retrieval, plus contested-ness.

6. The M dataset ends on 1 October 2026 [S1]. The UI must say "dataset end (1 Oct 2026)" instead of "now", and any comparison with live Wikipedia must use only revisions up to that date.

## 4. The product

Hindsight answers questions about Finnish history from Wikipedia as it stood on any date, shows which claims were fought over, and checks any AI answer against the record of that date.

| Act | What the user sees | Built on |
|---|---|---|
| Ask | A question in any language gets an answer with verified quotes and `oldid` permalinks. Finnish evidence is translated. | S vectors route, chunk vectors rank, FULLTEXT hybrid |
| Travel | A date slider plus "then vs now": which claims behind the answer were edited, added, removed or revived | `claim` and `layout` (system-versioned), `AS OF` |
| Trust | X-ray: claim by month heatmap, fault lines with one-line dispute summaries, zombie claims, uncited stretches. Audit: paste a bare-LLM answer and get supported, contradicted or absent per claim, plus the Wikipedia year it matches best. | `claim_stats`, audit pipeline |
| Stretch | Overlay on live fi.wikipedia; fi/sv/en coverage-gap map | `/api/match`, S text |

Readers get Ask, Travel and Audit. Editors get the X-ray, uncited stretches and the overlay. No Finnish is needed: questions and answers use your language, and evidence is quoted in Finnish with a translation.

Positioning against prior art.

| Existing tool | What it answers | What Hindsight adds |
|---|---|---|
| Who Wrote That?, WhoColor, XTools Authorship and Blame (WikiWho) [S7] | Who wrote which words | Claims as semantic units over time, retrieval by meaning, contested-ness |
| SVT starter time machine [S2] | What an article said on a date; when reverts happened | Which paragraph, what the dispute was, queryable by meaning |
| RAG starter [S3] | Which article answers a question | Which paragraph, as of which date, with verified quotes |

What it is not: a ranking of editors. The tool is about claims, not people (see section 11).

## 5. Architecture and data model

Everything lives in one MariaDB instance with two schemas, so a single SQL statement can route with vectors, travel in time and rank exactly.

```
 S dataset                M dataset                L dataset
 fi/sv/en + vectors       862k versions            568k articles
      |                        |                        .
      |                        v                        v  (stretch)
      |        +----------------------------------------------------+
      |        | OFFLINE PIPELINE                                   |
      |        | Chunk+hash -> Lineage -> Embed -> Load             |
      |        | (CPU)         diff+cosine  bge-m3   insert_history |
      |        +------------------------+---------------------------+
      |                                 |
      v                                 v
 +-----------------------------------------------------------------------+
 | MariaDB: one instance, two schemas                                    |
 |                                                                       |
 |  wikirag           wikihistory                                        |
 |  S vectors         page_state        claims + layout     chunks       |
 |  HNSW, cosine      SVT, all of M     SVT                 vectors+FTS  |
 +----------------------------------+------------------------------------+
                                    |
                         +----------v-----------+
                         | FastAPI service      |
                         | route, AS OF, rank,  |
                         | LLM                  |
                         +----------+-----------+
                                    |
                     +--------------+--------------+
                     v                             v
              Web app                      Wikipedia userscript
              ask, travel, X-ray, audit    overlay on live fi.wikipedia
```

S loads with the RAG repo's own scripts. The dashed arrow (L) is the optional scale test.

### Design decisions

1. Claims are rows, rewrites are history rows. A paragraph keeps its `claim_id` across edits and moves; unchanged paragraphs cost nothing. The SVT starter itself concludes that a full copy per version does not scale and that text should be stored once per distinct version [S2].
2. Reading order lives in `layout`, one small versioned row per article, not in a `pos` column. Otherwise inserting one paragraph would version every row below it.
3. Vectors are content-addressed in `chunk`: one row per distinct text, with HNSW and FULLTEXT there. History rows never duplicate a vector.
4. Retrieval has two stages. Stage 1 routes to pages by three signals: article-level ANN on S's vectors (R1), chunk-level ANN mapped to pages through the claim history (R2), and a lexical prefix match for names and years (R3). Stage 2 ranks exactly, with cosine over the claims alive on the chosen date in the routed pages (around a thousand vectors). This is exact, fast, and immune to the history-rows-in-the-index issue. An ANN-with-oversampling path over `chunk` serves unrouted queries, and you measure its recall against the exact path.
5. It is a funnel. `page_state` is a metadata-only system-versioned table for all 862k versions (cheap, whole corpus, powers triage and analytics). Claims, layout and vectors are built only for the pages that deserve them, from 50 to a few hundred.

### DDL

```sql
-- schema wikihistory (dataset S lives in schema wikirag, loaded by the RAG repo's own scripts)

CREATE TABLE page_state (                      -- corpus layer: metadata of every version, real edit times
  page_id   INT UNSIGNED NOT NULL,
  title     VARCHAR(255) NOT NULL,             -- titles change over time (page moves)
  rev_id    INT UNSIGNED NOT NULL,
  editor    VARCHAR(255), anon BOOL NOT NULL, minor BOOL NOT NULL,
  comment   TEXT, sha1 CHAR(31), bytes INT UNSIGNED,
  is_revert BOOL NOT NULL DEFAULT 0,           -- computed at load; never UPDATE a versioned row later
  undone    BOOL NOT NULL DEFAULT 0,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id), KEY (page_id, row_start), KEY (sha1), KEY (rev_id)
) WITH SYSTEM VERSIONING;

CREATE TABLE chunk (                           -- every distinct paragraph text, stored once
  text_hash BIGINT UNSIGNED PRIMARY KEY,       -- xxh64 of normalised text
  text      TEXT NOT NULL,
  emb       VECTOR(1024) NOT NULL,             -- bge-m3, same model as S
  VECTOR INDEX (emb) M=16 DISTANCE=cosine,
  FULLTEXT (text)
);

CREATE TABLE claim (                           -- one row per claim lineage; each rewrite is a history row
  page_id   INT UNSIGNED NOT NULL,
  claim_id  INT UNSIGNED NOT NULL,
  text_hash BIGINT UNSIGNED NOT NULL,
  section   VARCHAR(255),
  rev_id    INT UNSIGNED NOT NULL,             -- revision that wrote this state
  editor    VARCHAR(255), anon BOOL NOT NULL,
  n_refs    SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  comment   TEXT,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id, claim_id),
  KEY (page_id, row_start),                    -- the starter's measured fix for slow old-date AS OF [S2]
  KEY (text_hash)
) WITH SYSTEM VERSIONING;

CREATE TABLE layout (                          -- reading order, one tiny row per article
  page_id     INT UNSIGNED PRIMARY KEY,
  rev_id      INT UNSIGNED NOT NULL,
  claim_order JSON NOT NULL,                   -- [claim_id, ...]
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)
) WITH SYSTEM VERSIONING;

CREATE TABLE claim_stats (                     -- plain table: signals and summaries per claim
  page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL,
  n_states SMALLINT UNSIGNED, n_edits SMALLINT UNSIGNED, n_named SMALLINT UNSIGNED, n_anon SMALLINT UNSIGNED,
  flips SMALLINT UNSIGNED, named_flips SMALLINT UNSIGNED, alternation SMALLINT UNSIGNED,
  revivals SMALLINT UNSIGNED, named_reverts SMALLINT UNSIGNED, uncited_years DECIMAL(4,1),
  first_ts TIMESTAMP(6) NULL, last_ts TIMESTAMP(6) NULL,
  contest FLOAT, kind VARCHAR(16), summary TEXT,
  PRIMARY KEY (page_id, claim_id)
);
```

### The headline query: route, travel, rank

One statement routes with three signals, travels to a date, then ranks exactly. The SQL structure (CTE chain, cross-schema join, `AS OF` inside a CTE, derived-table `LIMIT` workaround, FULLTEXT prefix) was executed on MariaDB 10.11 with numeric stand-ins for the vector distances; the vector functions themselves were not (section 13).

```sql
SET mhnsw_ef_search = 80;
WITH r1 AS (                                   -- article-level ANN on S
  SELECT page_id FROM (
    SELECT page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS d
    FROM wikirag.article_vector WHERE lang = 'fi' ORDER BY d LIMIT 12) AS a),
r2 AS (                                        -- pages that ever held a text close to the question
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk
        ORDER BY VEC_DISTANCE_COSINE(emb, VEC_FromText(?)) LIMIT 100) AS n)),
r3 AS (                                        -- lexical prefix match for names and years
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk
        WHERE MATCH(text) AGAINST (? IN BOOLEAN MODE) LIMIT 200) AS m)),   -- e.g. 'mannerheim*'
routed AS (SELECT page_id FROM (
        SELECT page_id FROM r1 UNION SELECT page_id FROM r2 UNION SELECT page_id FROM r3) u LIMIT 25),
snap AS (                                      -- claims alive on that date
  SELECT page_id, claim_id, text_hash, section, rev_id, editor
  FROM wikihistory.claim FOR SYSTEM_TIME AS OF TIMESTAMP '2012-03-01 00:00:00'
  WHERE page_id IN (SELECT page_id FROM routed))
SELECT s.page_id, s.claim_id, s.section, s.rev_id, s.editor, c.text,
       VEC_DISTANCE_COSINE(c.emb, VEC_FromText(?)) AS d
FROM snap s JOIN wikihistory.chunk c ON c.text_hash = s.text_hash
ORDER BY d LIMIT 8;
```

Notes.
- Bind order: question vector (r1), question vector (r2), lexical string (r3), question vector (final). In Python you can also pass `array('f', vec).tobytes()` with `VEC_DISTANCE(…, ?)` [S4].
- For the "now" side of a comparison, drop the `FOR SYSTEM_TIME` clause; label it "dataset end (1 Oct 2026)" in the UI.
- `LIMIT` directly inside `IN (SELECT …)` is rejected by MariaDB (error 1235); wrap it in a derived table, as above.
- A table alias goes after the `FOR SYSTEM_TIME` clause (`FROM claim FOR SYSTEM_TIME AS OF … AS k`); the other order is a syntax error.
- If the union exceeds 25 pages, trim in the API layer by best rank (the SQL `LIMIT 25` is arbitrary).
- Per-page date snapping (see Step 6) means building the snap CTE as a `UNION ALL` of per-page `AS OF` subqueries.
- The starter found that a flat join defeats the vector index and that `ORDER BY distance LIMIT` must sit in a subquery joined afterwards [S3]; that is why every vector ranking above is in its own derived table or final exact scan.

---

## 6. Execution, step by step

Hours count from the start of hacking. Each step names its owner and the features (section 7) that validate it. Code in this section was executed unless it says otherwise (section 13).

### Step 0 (H0 to H2, everyone): smoke-test what can sink you

Validated by F1.

```bash
docker run -d --name hindsight -p 3306:3306 -e MARIADB_ROOT_PASSWORD=dev mariadb:lts \
  --max-allowed-packet=256M --innodb-buffer-pool-size=4G --character-set-server=utf8mb4
gh release download m-2026-10 -R MariaDB/mariadb-wikipedia-datasets      # M: 16 Parquet files, 315 MB [S1]
```

- Create schemas `wikirag` and `wikihistory`. Point both starter repos' `.env` at this instance; their scripts read DB_HOST, DB_PORT, DB_USER, DB_PASSWORD and DB_NAME [S3]. Run the RAG repo's setup (about three minutes with the ready-made vectors [S3]).
- Repo skeleton: `db/ pipeline/ api/ web/ eval/ docs/findings.md`.
- Run T1 to T5 below, and the pitfall script in Appendix C.

```sql
-- T1 backdated history rows (executed on 10.11: passes)
SET time_zone='+00:00'; SET system_versioning_insert_history=1;
CREATE TABLE t1 (id INT PRIMARY KEY, v VARCHAR(9),
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)) WITH SYSTEM VERSIONING;
INSERT INTO t1 (id,v,row_start,row_end) VALUES (1,'old','2010-01-01 00:00:00','2012-06-01 00:00:00');
INSERT INTO t1 (id,v,row_start)         VALUES (1,'new','2012-06-01 00:00:00');
SELECT v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2011-01-01 00:00:00';   -- expect: old

-- T2 HNSW + FULLTEXT on one table (needs MariaDB 11.7 or later; NOT executed in the sandbox)
CREATE TABLE t2 (h BIGINT UNSIGNED PRIMARY KEY, txt TEXT, e VECTOR(4) NOT NULL,
  VECTOR INDEX (e) M=16 DISTANCE=cosine, FULLTEXT (txt));

-- T3 HNSW on a versioned table (needs 11.7 or later; NOT executed): does it create, and after
-- updates and deletes does AS OF ... ORDER BY VEC_DISTANCE_COSINE ... LIMIT 5 still return 5 rows?
CREATE TABLE t3 (id INT PRIMARY KEY, e VECTOR(4) NOT NULL,
  VECTOR INDEX (e) DISTANCE=cosine) WITH SYSTEM VERSIONING;
```

- T4: run the headline query (section 5) on toy rows in both schemas.
- T5: embed 50 sentences with Ollama's bge-m3 and with sentence-transformers. The cosine between the two must be at least 0.99, because S's vectors came from Ollama [S1] and your chunk vectors will probably come from a GPU box.

### Step 1 (H2 to H5, data/DB): corpus layer, `page_state` for all of M

Validated by F2.

- Read the Parquet files with DuckDB. Insert with real times: one transaction per page, batches capped by bytes (the default packet is 16 MB), one worker per Parquet file, skip pages already loaded.
- Compute `is_revert`, `undone` and `anon` at load time. Never `UPDATE` a versioned row to add them later: an update writes a history row stamped with the real clock (confirmed in the sandbox).
- Per-row speed should beat the starter, which loaded 31,848 versions with 2 GB of text in 17 min 40 s [S2]. The total for 862,076 rows is a number to record (est.: tens of minutes with parallel workers).

```python
from datetime import timedelta

def history_rows(versions):            # one page, sorted by (timestamp, rev_id)
    starts, last = [], None
    for v in versions:                 # same-second edits: +1 microsecond, order preserved
        s = v["timestamp"] if last is None or v["timestamp"] > last else last + timedelta(microseconds=1)
        starts.append(s); last = s
    for i, v in enumerate(versions):   # end=None -> current row: INSERT without row_end
        yield v, starts[i], (starts[i + 1] if i + 1 < len(versions) else None)
```

The bump is mandatory, not cosmetic: a history row whose `row_start` equals its `row_end` is rejected by MariaDB (error 1525, sandbox-verified). Use the same bumped timestamps for `page_state`, `claim` and `layout`, otherwise `AS OF` joins across them drift.

```sql
SET time_zone='+00:00'; SET system_versioning_insert_history=1;
INSERT INTO page_state (page_id,title,rev_id,editor,anon,minor,comment,sha1,bytes,is_revert,undone,row_start,row_end) VALUES (...);  -- history rows
INSERT INTO page_state (page_id,title,rev_id,editor,anon,minor,comment,sha1,bytes,is_revert,undone,row_start)         VALUES (...);  -- the current row
```

Revert flags (checked against the starter-style window query and on golden cases):

```python
def flags(sha1s, times, max_span=timedelta(hours=48)):
    """is_revert[i]: sha1 equals an earlier version's but not the previous one's (the starter's rule [S2]).
       undone[j]: j sits between the closest earlier identical version a and the reverting version b,
       unless a was itself undone (repeat of vandalism) or the span is longer than max_span."""
    n = len(sha1s); is_rev = [False]*n; undone = [False]*n; last_seen = {}
    for i, s in enumerate(sha1s):
        if s in last_seen and (i == 0 or s != sha1s[i-1]):
            is_rev[i] = True
            a = last_seen[s]
            if not undone[a] and times[i] - times[a] <= max_span:
                for j in range(a + 1, i): undone[j] = True
        last_seen[s] = i
    return is_rev, undone
```

Why the two extra conditions: without them, repeated identical vandalism marks the good edits between two blankings as undone, and a long-range revert to an old version marks years of legitimate edits as cleanup.

Then triage in SQL: rank pages by named reverts per 100 versions and by edit bursts. Hero set = the starter's 50 articles (the ones behind its test questions, the five presidents and 13 of the longest [S2]) plus whatever triage flags. That is the funnel: 19.7k pages, then about 200 candidates, then 50 deep.

### Step 2 (H3 to H8, ML): chunk and hash

Validated by F3.

- Split raw wikitext on blank lines and headings (headings set `section`). Count `<ref` tags per paragraph before cleaning, so citation density comes for free.
- Clean with the starter's `history.py` rules (templates, tables and references dropped, links turned into words [S2]) instead of writing a new cleaner.
- Drop infobox, table, category and file lines. Merge short paragraphs within a section up to about 900 characters; split long ones on sentences at about 1,500.
- Normalise, then hash. `xxhash` needs bytes, and the 64-bit integer fits `BIGINT UNSIGNED`:

```python
import re, unicodedata, xxhash
def norm(s): return re.sub(r"\W+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()
def text_hash(s): return xxhash.xxh64_intdigest(norm(s).encode("utf-8"))
```

- Process pages in parallel. Revisions within a page are sequential.
- Output: `(page, rev) -> [text_hash...]` in Parquet, and N, the number of unique hashes. N sizes the embedding job.

### Step 3 (H5 to H12, ML): lineage resolver

Validated by F4. The function below and its tests (Appendix A) were executed.

Resolve each revision against the previous one in four moves: (1) an exact hash diff keeps unchanged paragraphs; (2) an optimal one-to-one assignment on character similarity (at least 0.60) catches in-place rewrites and moves; (3) unmatched old paragraphs are deleted and parked in a graveyard; (4) unmatched new ones are revived from the graveyard (same text as a deleted claim) or added as new claims.

```python
from difflib import SequenceMatcher
from rapidfuzz.fuzz import ratio
from scipy.optimize import linear_sum_assignment
import numpy as np

def resolve(prev, cur, grave, new_id, t_edit=0.60):
    """prev: [(claim_id, hash, text)] of rev i-1; cur: [(hash, text)] of rev i; grave: {hash: claim_id}."""
    ids, events = [None] * len(cur), []
    sm = SequenceMatcher(None, [p[1] for p in prev], [c[0] for c in cur], autojunk=False)
    gone, fresh = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ids[j1:j2] = [p[0] for p in prev[i1:i2]]
        else:
            gone += range(i1, i2); fresh += range(j1, j2)
    if gone and fresh:                                   # rewritten in place or moved
        S = np.array([[ratio(prev[o][2], cur[n][1]) / 100 for n in fresh] for o in gone])
        for a, b in zip(*linear_sum_assignment(-S)):
            if S[a, b] >= t_edit:
                ids[fresh[b]] = prev[gone[a]][0]; events.append(("edit", ids[fresh[b]], S[a, b]))
    used = {i for i in ids if i is not None}
    for o in gone:                                       # no successor: deleted
        if prev[o][0] not in used:
            events.append(("delete", prev[o][0], None)); grave[prev[o][1]] = prev[o][0]
    for n in fresh:                                      # no predecessor: revival or brand new
        if ids[n] is None:
            h = cur[n][0]
            if h in grave and grave[h] not in used:
                ids[n] = grave.pop(h); events.append(("revive", ids[n], None))
            else:
                ids[n] = new_id(); events.append(("add", ids[n], None))
            used.add(ids[n])
    return ids, events
```

- The first revision goes through the same function with `prev = []`: everything is an add.
- A moved paragraph shows up as an `edit` event with similarity 1.0 and keeps its id.
- Semantic pass (after Step 4): within the same section and within two revisions, merge a deleted claim and a new one if cosine is at least 0.88, so rewording that character similarity misses is linked. In the unit tests a realistic rewrite scored 0.57, just under the 0.60 threshold, and came out as delete plus add; that is exactly the gap this pass closes. Sweep both thresholds in F4.
- Turn resolver output into history rows with a per-claim open state (hash, start, rev, editor): close on edit or delete, open on add or revive; open states at the end become current rows. The `layout` row follows the same pattern. The tested bookkeeping is in Appendix B.
- Page-blanking vandalism multiplies row-versions (every claim is deleted and then revived). In the toy history, 3% blank-and-restore episodes pushed the store to 2.40 claim row-versions per revision. Optional: collapse undone anonymous bulk deletions out of the claim store.

### Step 4 (H6 to H14, ML and data): embed only what is new

Validated by F5.

- Embed unique hashes only. If a new state has character similarity of at least 0.97 to its parent (a typo fix), copy the parent's vector under the new hash and skip the model call.
- Compute check: the starter measured about 1,400 tokens per second through Ollama [S3]. At roughly 150 tokens per chunk that is about 9 chunks per second on a laptop, so 30k chunks is about an hour in the background. If N is above about 100k, get a GPU by H6 (ask the mentors, or use a free T4 on Kaggle or Colab). Expect 10 to 50 times faster with batched fp16 (est.; measure it). N should be on the order of 10^4 to 10^5 for 50 articles (est.), since they hold 31,848 versions [S2].
- Use the same model as S. With Ollama, set both `num_ctx` and `num_batch`; setting only the context length lets longer inputs be refused or silently cut [S3].

```python
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("BAAI/bge-m3", device="cuda"); m.max_seq_length = 512; m.half()
E = m.encode(texts_sorted_by_length, batch_size=64, normalize_embeddings=True)
cur.execute("INSERT INTO chunk (text_hash,text,emb) VALUES (?,?,?)", (h, t, E[i].astype("float32").tobytes()))
```

- HNSW recall depends on insertion order [S3], so re-measure recall after the final load.

### Step 5 (H10 to H14, data/DB): load the deep layer

Validated by F6.

Write `claim` and `layout` with the same `insert_history` pattern as Step 1, using the same bumped timestamps. Then run the round-trip test on the loaded tables. The reconstruction function (executed on the toy store with `%s` placeholders for pymysql; use `?` with the `mariadb` connector):

```python
import json
def reconstruct(cur, page_id, ts):                 # ts: full 'YYYY-MM-DD HH:MM:SS.ffffff'
    cur.execute("SELECT claim_order FROM layout FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id=?", (ts, page_id))
    order = json.loads(cur.fetchone()[0])
    cur.execute("SELECT claim_id, text_hash FROM claim FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id=?", (ts, page_id))
    h = dict(cur.fetchall())
    return [h[c] for c in order]                   # must equal the chunker's hash list for that revision
```

Then measure storage for the same 50 pages. The full-copy design took 2,049 MB of wikitext, 1,202 MB on disk with compressed columns [S2]. Compare claims plus layout plus chunk text, then add vectors.

### Step 6 (H12 to H20, backend and LLM): retrieval, `/api/ask`, then versus now

Validated by F7, F8 and F9.

- Embed the question with the same service. Run the three-signal headline query (section 5) for the date and, if requested, for the dataset end. Classify each claim as unchanged, edited, removed, added or revived by comparing the two sets. Send the LLM only this delta plus evidence, not whole pages.
- Per-page date snapping: if the revision live at `t` was undone, answer from the last surviving one, otherwise a slider can land on vandalism. Executed on the toy history: 1,000 of 1,000 snaps correct; in the 41 instants that fell inside a vandalised window, plain `AS OF` returned the vandalised state every time and the snapped query returned the pre-vandalism state every time.

```sql
SELECT MAX(row_start) AS snap_ts
FROM page_state FOR SYSTEM_TIME ALL
WHERE page_id = ? AND row_start <= ? AND undone = 0;
-- then: claim / layout FOR SYSTEM_TIME AS OF TIMESTAMP <snap_ts>
```

- Hybrid, once the baseline passes: FULLTEXT on `chunk.text` in boolean mode with prefix terms (`mannerheim*`), fused with the vector rank by Reciprocal Rank Fusion (k=60). The starter found that FULLTEXT has no Finnish stemming (the bare name and two inflected forms matched 649, 305 and 43 articles) [S3]. In a sandbox test the prefix term matched both the bare and the inflected form.
- Prompt rules: answer only from numbered evidence; cite every sentence as `[n]`; quote at most 25 words; answer in the question's language; refuse if evidence is thin. Programmatically check that every cited quote appears (normalised) in the cited chunk, and drop or regenerate otherwise. Link each citation to `https://fi.wikipedia.org/?oldid=<rev_id>`.

```python
import re, unicodedata
norm = lambda s: re.sub(r"\W+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()
quote_ok = lambda quote, chunk_text: norm(quote) in norm(chunk_text)
```

- Endpoints (freeze the contract at H3):

```
POST /api/ask      {q, as_of, compare_to?, lang?}  -> answer[], evidence[], delta[], sql, timings
GET  /api/xray/{page_id}?from&to                   -> claims[] with monthly cells, contest, flips, revivals
GET  /api/claim/{page_id}/{claim_id}                -> states[] (ts, rev_id, editor handle, anon, comment, text), summary
POST /api/audit    {q, as_of}                       -> llm_answer, claims[] {verdict, evidence, vintage[]}
GET  /api/snapshot/{page_id}?t=                     -> article rebuilt from claim + layout
POST /api/match    {title, paragraphs[]}            -> per paragraph: claim_id, contest, badges   (overlay)
```

### Step 7 (H16 to H26, ML and backend): contested-ness, X-ray, dispute summaries

Validated by F10, F11 and F12. Compute signals in SQL where possible so MariaDB does the work.

| Signal | Definition |
|---|---|
| flips | state returns to an earlier hash (A, B, A) |
| named flips | the same, counting only rows where the editor and the previous row's editor are both named |
| alternation | longest A/B/A/B run of two named editors on the claim |
| revivals | deleted, then back |
| named reverts | identity reverts by named editors that touch the claim |
| churn | distinct states per year alive |
| uncited stretch | years alive with `n_refs = 0` |

```sql
-- flips per claim (executed on 10.11: A,B,A,B,A gives 3; A,B,A gives 1)
SELECT claim_id, SUM(text_hash = h2 AND text_hash <> h1) AS flips
FROM (SELECT page_id, claim_id, text_hash,
             LAG(text_hash,1) OVER w AS h1, LAG(text_hash,2) OVER w AS h2
      FROM claim FOR SYSTEM_TIME ALL WHERE page_id = ?
      WINDOW w AS (PARTITION BY page_id, claim_id ORDER BY row_start)) x
GROUP BY claim_id;

-- X-ray cells: claim by month (not executed; plain SQL over the history)
SELECT claim_id, DATE_FORMAT(row_start, '%Y-%m') AS month, COUNT(*) AS edits, SUM(anon) AS anon_edits
FROM claim FOR SYSTEM_TIME ALL WHERE page_id = ? GROUP BY claim_id, month;
```

- Contest score: rank-average four signals (named flips, alternation, revivals, named reverts) within the article and squash to [0,1]. Keep the starter's grey versus red split: anonymous cleanup versus named-against-named [S2].
- Validate by hand-labelling 30 claims (15 minutes, whole team) and reporting precision@10.
- Dispute summaries: for the top ten claims per article, one LLM call on the distinct states with dates and edit summaries. Return JSON `{topic, side_a, side_b, kind: factual|wording|naming|sourcing|cleanup}`. The prompt forbids guessing editors' motives or politics.

### Step 8 (H24 to H32, backend and ML): audit and vintage

Validated by F13.

1. Get the bare-LLM answer (no retrieval).
2. Extract at most 12 atomic claims as JSON.
3. For each claim: embed, route, then exact `AS OF` top 3.
4. A small model returns supported, contradicted or absent plus a quote; the quote is verified programmatically.
5. Vintage: repeat step 3 for each year-end from 2005 to 2026 and plot the best cosine per year. The peak is the Wikipedia year the claim matches best.

Label the strip "closest year", not "truth": it is similarity, not entailment. That a model's vintage peaks near its training period is a hypothesis, not a result. It is also the cheapest answer to the brief's point that LLMs carry a worldview different from Wikipedia's NPOV, and only the time axis makes it possible.

### Step 9 (H3 to H34, frontend and story): UI

Validated by F14.

- Contract-first: build on JSON fixtures from H3 and switch to live data at H14. Without a frontend person, fork the SVT starter's app: it draws its timeline server-side as SVG with no JavaScript [S2].
- Ask view: one input, a date slider with an edit-activity sparkline from `page_state`, a then/now toggle. The answer streams. Citation chips open a card with the Finnish paragraph, translation, date, editor class, contest badge and verified tick.
- X-ray: a canvas heatmap with rows as claims in current reading order and columns as months, grey outline for anonymous cleanup, red for named conflicts, plus a "fault lines" list. Clicking a row opens the claim biography with a diff between states.
- Audit: verdict chips plus the vintage strip.
- Include an SQL drawer with timings (the starters show the SQL under results). Use a colour-blind-safe palette (blue to orange for intensity, red only for named conflicts), hatching as well as colour, and state in the URL for permalinks.

### Step 10 (H26 to H36, data/DB): evaluation and scale numbers

Validated by F15.

- Retrieval: run the starter's 37 test questions (`data/test-questions.tsv`) for article vectors, routed chunks and hybrid, reporting hit@1/5/10 and MRR. Call out the five president questions.
- Temporal set: build 40 questions from claims with at least two meaningfully different states (LLM-written, human spot-check). Measure whether the right `text_hash` is in the top 5 at the right date, against a current-only baseline. The authors say more test questions would make their numbers more trustworthy [S3], so publish the set as CSV and add `make bench`.
- Index: recall@10 of the ANN path against exact for ef 20, 80 and 160. The starter found the defaults (M=6, ef 20) at 79% recall and M=16, ef 80 at 100% [S3]. Also sweep `mhnsw_max_cache_size`: the starter left it at its default and marked its effect unverified [S3].
- `AS OF` latency p50 and p95 with and without `KEY(page_id, row_start)`, plus the `page_state` load time, size and triage latency.
- Stretch L: embed lead sections of the 568k Finnish articles on a GPU. The starter's index rebuild took 3.5 minutes for about 28k vectors [S3], so budget at least an hour at 568k (est., linear extrapolation).

### Step 11 (stretch, 3 hours each): overlay and gap map

Validated by F16. Start only after gate G3.

- Overlay: a Tampermonkey userscript on `https://fi.wikipedia.org/wiki/*`. It sends paragraph texts to `/api/match` via `GM_xmlhttpRequest` (sidesteps CORS and CSP); the server embeds them, finds the nearest current claim by cosine, and the script paints a left border by contest score with a tooltip. About 40 lines of JavaScript and 40 of Python. It answers the RAG starter's own open question about whether this could live inside Wikipedia [S3].
- Gap map: for 3 to 5 topics present in fi, sv and en in S, chunk the Markdown, embed it, and flag fi paragraphs whose best match in another language falls below about 0.6. That gives editors a coverage matrix.

### Step 12 (H36 to H44, everyone): freeze and ship

Validated by F17.

- Feature freeze at H36.
- Cache demo prompts in an `llm_cache` table in MariaDB and add a replay mode, in case venue Wi-Fi dies.
- README with architecture, measured numbers and licence note: Wikipedia text and anything derived from it is CC BY-SA 4.0, so credit and share alike [S1].
- 90-second backup video and three rehearsals.
- Submit by H42, leaving two hours of slack.

---

## 7. Feature specs and validation (F1 to F17)

### How to read this

Every feature has four parts.

- Does: its job and boundary.
- Verifies: the checks it must run. Each is one of three kinds. An invariant must hold on every row, and any violation is a bug. An oracle is compared against an independent source of truth (the starters' published numbers, live Wikipedia, or a reference implementation). A golden set is a small, fixed, labelled sample.
- Expect: what a healthy run looks like, including the warning sign that something is wrong. Numbers come from the starters where published, and my own estimates are marked "est.".
- Valid and correct when: the pass condition. Valid means internally consistent. Correct means it agrees with an oracle or a labelled set.

Thresholds are targets I chose, and the samples are hackathon-sized, so always report x of n. If you miss a threshold, publish the measured number and the limitation instead of tuning the test. Log every result in `docs/validation.md` (check, number, commit, date). A feature may be demoed only while its line is green. If it is red at its gate, fix it or cut it and list it under known limitations.

## A. Data layer

### F1. Environment and smoke tests (Step 0, everyone, gate H2)

Does. One `mariadb:lts` instance with schemas `wikirag` and `wikihistory`. S is loaded with the RAG repo's scripts, M is downloaded, and five smoke tests decide design branches.

Verifies.
1. The release files match their `SHA256SUMS`, which every release ships with [S1].
2. S counts are exact: 29,460 articles, each with a vector [S3], split 19,285 fi, 4,921 sv and 5,254 en [S1].
3. Oracle: the RAG repo's `evaluate.py` on your instance reproduces its published numbers. On the 37 questions that is hit@1/5/10 of 57/76/86% and MRR 0.65 [S3].
4. T1 returns `old` (passes in the sandbox). T2 creates and answers both a MATCH and a vector query. T3 may succeed, fail, or return fewer than 5 rows. T4 runs the headline query on toy rows across both schemas. T5 gives cosine at least 0.99 between Ollama and sentence-transformers on 50 texts, because S's vectors were made by Ollama [S1]. T2 and T3 need MariaDB 11.7 or later and were not executed in the sandbox (which ran 10.11).
5. `SELECT VERSION()` is at least 11.7, `system_versioning_insert_history` can be set by an ordinary user (confirmed on 10.11, and the starter says the same [S2]), and `max_allowed_packet` is at least 64 MB.

Expect. S loads in about three minutes, as the starter's setup did [S3]. The evaluation reproduces within a few points, because HNSW graphs differ with insertion order [S3]. T3 is the only test where several outcomes are acceptable. A failing T1 is not acceptable.

Valid and correct when. Checksums are OK, counts are exact, and the baseline is reproduced within 3 percentage points on each of hit@1, hit@5 and hit@10. T1, T2 and T4 pass. T3 and T5 outcomes are written down with the decision each triggers. A T3 failure just means the ANN path over versioned tables is off the table. A T5 result below 0.99 means one embedding service for everything.

### F2. Corpus layer `page_state` and triage (Step 1, data/DB, gate H5)

Does. Loads the metadata of all 862,076 versions of 19,742 pages as a system-versioned table with real UTC edit times. It derives `is_revert`, `undone` and `anon` at load time and ranks pages to choose the hero set.

Verifies.
1. Counts (invariant): `COUNT(*)` over `FOR SYSTEM_TIME ALL` is 862,076, there are 19,742 distinct pages, and per-page counts equal the Parquet counts (0 mismatches).
2. Chain (invariant, executed on the toy store: 0 broken):

```sql
WITH h AS (
  SELECT page_id, row_start, row_end,
         LEAD(row_start) OVER (PARTITION BY page_id ORDER BY row_start) AS next_start
  FROM page_state FOR SYSTEM_TIME ALL)
SELECT COUNT(*) AS broken FROM h
WHERE row_end <= row_start OR (next_start IS NOT NULL AND row_end <> next_start);   -- expect 0
```

   Also, `SELECT COUNT(*) - COUNT(DISTINCT page_id) FROM page_state` (current rows only) must be 0.
3. Point-in-time identity (invariant): for 10,000 random revisions, `AS OF` its timestamp returns exactly that `rev_id`, and 1 microsecond earlier returns its predecessor.
4. Oracle on the same dump. The starter's 50 pages hold 31,848 versions, with 3,357 for Suomen sisällissota and 2,143 for Talvisota. Talvisota has 328 reverts, 240 of which undo an anonymous editor [S2]. Your 328 must match exactly, because it is a pure sha1 rule: a version equal to an earlier one but not to the one just before [S2]. The 240/88 split depends on who counts as the undone editor, so copy the starter's rule, then match it or document the gap.
5. Sanity: anonymous editors account for 19% of versions [S1], so accept 17 to 21%.
6. Golden tests for the revert flags (all executed): A,B,C,A' flags A' as a revert and B and C as undone; A,V,A,V,A flags only the V's (no good edit); A,V,A,X,V leaves X standing; a revert with a span over 48 hours flags nothing as undone; A,A,B,B has no reverts. On the toy history, `is_revert` equals the starter-style window query (81: 41 restores plus 40 repeated identical blankings, which the starter's rule counts as reverts) and `undone` flags exactly the 41 blanking revisions with 0 good revisions wrongly flagged.
7. Five revisions compared with the live fi.wikipedia history (UTC), restricted to dates before 1 Oct 2026.

Expect. A load of tens of minutes (est.). The starter needed 17 min 40 s for 31,848 versions with 2 GB of text [S2], and yours has no text. The table is a few hundred MB (est.), triage queries return in seconds, and the heavy pages (Suomen sisällissota, Talvisota, the presidents) rank near the top. Warning sign: a count off by a few hundred means ordering or hidden-revision handling. A broken chain means the same-second bump.

Valid and correct when. Checks 1 to 3 show 0 violations. Check 4 matches exactly, or the deviation is traced to a definition and documented. Check 6 passes, and check 7 is 5 of 5. The hero list is frozen in `docs/hero.md` and contains all 37 gold pages of the starter's questions, which you must check.

### F3. Chunker and hashing (Step 2, ML, gate H8)

Does. Turns each revision's raw wikitext into ordered chunks `(section, text, n_refs, text_hash)`. It splits on blank lines and headings, counts `<ref` before cleaning, applies the starter's cleaning rules, merges short chunks and splits long ones, then normalises and hashes with xxh64.

Verifies.
1. Determinism (invariant): two runs, and serial versus parallel, give identical hashes on 5 pages.
2. Boundary stability (invariant, the most important): over 1,000 consecutive revision pairs, the median number of changed chunk hashes is at most 2 and P90 is at most 6. A cascade (a small edit re-merging its neighbours) would blow up both embedding cost and false churn.
3. Coverage: on 50 sampled revisions, chunk text accounts for at least 95% of the cleaned article's characters.
4. Markup-leak scan for `{{`, `[[`, `<ref` and `{|`: at most 0.5% of chunks hit, and you eyeball every hit.
5. `n_refs` equals a raw `<ref` count on 20 hand-checked paragraphs.
6. Sizes: at least 90% of chunks are 60 to 1,500 characters and none are empty. ä, ö and å survive a UTF-8 round trip.

Expect. Tens to low hundreds of chunks per revision on long articles (est.). N, the number of unique hashes, is on the order of 10^4 to 10^5 for 50 pages (est.), and it sizes F5. Warning sign: N far above 10^5 points to a boundary cascade.

Valid and correct when. Check 1 passes, check 2 meets both bounds, check 3 is at least 95% and check 4 is at most 0.5%. N is recorded in `docs/findings.md`. If check 2 fails, drop the merge rule and use fixed paragraphs before touching anything else.

### F4. Lineage resolver (Step 3, ML, gate H12)

Does. Gives each chunk a stable `claim_id` across revisions and emits events (edit, delete, revive, add). A semantic pass then merges rewrites at cosine of at least 0.88.

Verifies.
1. Synthetic unit tests (invariant; cases 1 to 9 were executed, Appendix A):

| Case | Change between revisions | Expected |
|---|---|---|
| Insert | one new paragraph | 1 add, other ids unchanged |
| Delete | one paragraph removed | 1 delete |
| Typo | one character | same id, edit, similarity at least 0.9 |
| Move | paragraph relocated | same id, similarity 1.0 |
| Rewrite | wording changed, same claim | pass 1: delete plus add. After the semantic pass: one edit, same id |
| Split | one paragraph becomes two | larger half keeps the id, the other is an add |
| Merge | two paragraphs become one | one id survives, the other is a delete |
| Blank and restore | page blanked, then reverted | all delete, then all revive with original ids, 0 new ids |
| Unrelated swap | two paragraphs replaced by two different ones | no cross-linking |

2. Invariants on every hero revision: `len(ids) == len(cur)`, ids are unique within a revision, every id is carried over, revived or new, and a replay gives identical output.
3. Golden set: two people each label 50 transitions. For linked pairs they ask "same claim?" (precision). For delete-plus-add pairs in one section they ask "should these be linked?" (recall). Sweep the thresholds (0.5, 0.6, 0.7 and 0.85, 0.88, 0.92) and choose by precision first.
4. Oracle: for 20 vandalism episodes (`undone` in `page_state`), the claim-id set after the reverting revision equals the pre-vandalism set exactly.
5. Accounting: per revision, every new hash is covered by exactly one add, revive or edit event (up to duplicate paragraphs).

Expect. 0 to 2 events per ordinary revision and delete/revive bursts around vandalism. The total number of claims ever seen is several times the current paragraph count on busy pages (est.), and a 3,357-revision page resolves in about a minute on one core (est.; the toy run resolved 1,242 revisions of 40 paragraphs in 0.5 s). Warning sign: more than about 20 times the current paragraph count indicates over-splitting.

Executed result. Eight of nine cases pass as specified; the rewrite case produced delete plus add at pass 1 (similarity 0.57 below the 0.60 threshold), as the plan predicts. A 400-revision simulation (typos, light rewrites, moves, inserts, deletes over 30 paragraphs) gave 0 id mismatches in 13,366 paragraph-revision pairs. Real wikitext is messier, so the labelled golden set (check 3) is the real test.

Valid and correct when. All 9 unit cases pass (the rewrite case after the semantic pass) and invariants show 0 violations on all hero pages. Labelled precision is at least 95% and recall at least 80%, and the vandalism oracle is 20 of 20. If precision is below 95%, raise the thresholds or ship without the semantic pass and say so. A false merge invents a history, while a missed merge only splits one.

### F5. Embedding pipeline and `chunk` store (Step 4, ML and data, gate H14)

Does. Embeds each unique hash once with bge-m3 and reuses the parent's vector for near-identical states (similarity at least 0.97). It loads `chunk` (HNSW plus FULLTEXT) and can be killed and resumed.

Verifies.
1. Completeness (invariant). The check below needs F6 loaded; before that, check against the chunker output. Orphans must be 0.

```sql
SELECT COUNT(*) FROM claim FOR SYSTEM_TIME ALL c
LEFT JOIN chunk k USING (text_hash) WHERE k.text_hash IS NULL;
```

2. Vector hygiene (invariant): no NaN or Inf, norms consistent across the table (cosine does not need unit norm), and self-distance below 1e-6.
3. Self-retrieval (invariant): for 200 random chunks, exact search (`IGNORE INDEX`) returns the chunk itself at rank 1 (200 of 200). The HNSW path does so for at least 99%.
4. T5 parity at least 0.99.
5. Oracle for the shortcut: for 200 copied vectors, a fresh embedding has cosine at least 0.98 with the copy for at least 99% of them.
6. Golden probes: 10 hand-written probes in fi, sv, en and fr (for example, which treaty ended the Winter War) each find the intended paragraph in the top 5, at least 9 of 10.
7. ANN recall@10 against exact on 200 queries at your chosen `M` and `mhnsw_ef_search` is at least 95%. The starter got 100% at M=16, ef 80, but only 79% at the defaults [S3].
8. Resumability: kill the job mid-run and restart it. Final counts are identical and there are no duplicates.

Expect. About 9 chunks per second on a laptop through Ollama (est., from the starter's roughly 1,400 tokens per second [S3] at about 150 tokens per chunk). A GPU should be 10 to 50 times faster (est.). Index build is about 3.5 minutes per 28k vectors [S3], so about 12 minutes at 100k (est.). Each vector takes 4 KB. Warning sign: throughput below 3 chunks per second means the Ollama `num_ctx`/`num_batch` trap. Recall below 90% means raising `M` or ef.

Valid and correct when. Checks 1 to 3 and 8 pass, check 4 is at least 0.99, check 5 is at least 99%, check 6 is at least 9 of 10, and check 7 is at least 95%. If recall is short, raise `M` or ef and record the latency cost.

### F6. Deep-layer load and reconstruction (Step 5, data/DB, gate H14)

Does. Writes `claim` and `layout` history rows with real (bumped) times. It proves the store can rebuild any article at any moment and measures storage against full copies.

Verifies. The reconstruction function is in Step 5.
1. Round trip, the main gate. Compare every revision at its own time and one microsecond before, plus 1,000 random instants, with the active revision in `page_state`. Cross-check every `revisions.n_chunks` against the rows emitted to `rev_chunks`. All must match exactly.
2. Layout and claim set equality (invariant): at every reconstructed time, the ids in `claim_order` equal the ids alive in `claim`.
3. History integrity (invariant): SQL checks periods, current rows, page-state timestamps, metadata, hashes and layout changes. Independently rebuild claim rows from `claim_ids` plus chunk output. This replaces the event-log opening count: claim states also open when exact spelling, citation count or section changes, which are not lineage events. Claims deleted and later revived legitimately have gaps, so do not require contiguity.
4. Idempotence (invariant): reloading on a clean database gives identical row counts and checksums for claim, layout and `text_variant`.
5. Oracle for storage: record MB for claim, layout, `chunk.text`, `text_variant.text`, vectors and FULLTEXT files. Compare raw wikitext with a cleaned-prose full-copy baseline and the deduplicated text stores separately for the same 50 pages.
6. Latency: time the two queries used by `reconstruct()` and gate on the optimizer's plan at p95 <= 10 ms. Record each query's `EXPLAIN` key across the timed cases, plus forced-key and no-key timings.
7. Fault injection: identify whether round trip, layout equality, SQL integrity or independent rebuild catches each loader fault, including a valid-time overlap, corrupt variant text and redundant layout rows.

Expect. Roughly 1 to 3 claim row-versions per revision (est.), so tens of thousands of rows for 31,848 versions. Page-blanking vandalism inflates the number, because every claim is deleted and then revived: the toy history with 3% blank-and-restore episodes gave 2.40 per revision. Text storage should land one to two orders of magnitude below full copies (est.), and total size is dominated by vectors. Warning sign: a round trip that fails only on same-second revisions means the bump logic.

Executed result (toy store, MariaDB 10.11, Appendix B). 1,242 synthetic revisions (357 needed the microsecond bump, 41 vandalism episodes) loaded into `page_state`, `claim` and `layout`. Round trip: 1,242 of 1,242 at revision times, 1,241 of 1,241 at 1 microsecond before, 1,000 of 1,000 at random instants. Chain breaks 0, overlapping claim periods 0, claim keys with more than one current row 0. Reconstruction took p50 0.9 ms and p95 1.45 ms for two queries on a toy table; the real number is yours to measure.

Valid and correct when. F6.1 through F6.4 pass, all F6.7 faults are caught by their specified sub-checks, and storage plus optimizer-plan latency are measured and recorded.

## B. Retrieval and answers

### F7. Retrieval: routing, `AS OF`, exact ranking, hybrid (Step 6, backend, gate H14)

Does. For a (question, date) pair it returns the top-k claims alive on that date.
- Stage 1 routes to pages by three signals, with the union capped at 25 pages: R1 is article-level ANN on S's vectors, R2 is chunk-level ANN on `chunk` mapped to pages through the claim history, R3 is a lexical prefix FULLTEXT match such as `mannerheim*`. Routing on article vectors alone would inherit the starter's president misses: if the right article never reaches stage 2, chunk vectors cannot rescue it.
- Stage 2 ranks exactly with `AS OF`, with each page's date snapped past vandalism.
- Lexical and vector ranks are optionally fused by RRF (k=60).

Verifies.
1. Time filter (invariant): over 1,000 random (question, date) pairs, every returned row satisfies row_start <= t < row_end (select the invisible columns by name). Its text also equals `reconstruct()` at t. 0 violations.
2. Appear and disappear (invariant): take 100 texts whose hash occurs on a page only during [a, b) and lives at least 2 days. Querying the text at a minus 1 day must not return it. At a plus 1 day it must return it at rank 3 or better. At b plus 1 second it must not return it. 100 of 100 on each boundary.
3. Router recall: on the 37 starter questions and the 40 temporal ones, report the share whose gold page is in the routed set, for R1 alone and for R1, R2 and R3 together.
4. Oracle for ranking: hit@1/5/10 and MRR at page level (page score is its best claim), against the starter's 57/76/86% and 0.65. Report as x of 37, because one question is about three points [S3].
5. President questions: the starter misses 3 of its 5 [S3], so report all five before and after.
6. Hybrid: MRR(hybrid) is at least MRR(vector) minus 0.02 overall and better on questions containing a proper name. Prefix test: `mannerheim*` returns at least as many chunks as each inflected form alone (in the sandbox it matched both forms). The starter saw the bare name, its genitive and its allative match 649, 305 and 43 articles [S3].
7. Query shape (invariant): `EXPLAIN` shows the vector index on the stage-1 subquery. `SHOW SESSION STATUS LIKE 'Handler_read%'` shows hundreds of rows read, not tens of thousands, which is the starter's own technique [S3].
8. Snapping: 20 vandalised revision timestamps plus 1 second return the pre-vandalism state (20 of 20). For 100 ordinary dates, snapping changes nothing. On the toy history: 1,000 of 1,000 snaps correct, and 41 of 41 vandalised instants repaired.
9. Multilingual: 10 questions each in en, fi and fr return the same gold page in the top 5 in at least 80% of pairs.
10. Latency (est. budgets): SQL stages p50 at most 200 ms and p95 at most 500 ms with at most 25 routed pages, and end to end without the LLM p95 at most 800 ms.

Expect. R1 takes about 5 ms and embedding the question about 30 ms, as the starter measured [S3]. Stage 2 takes 100 to 150 ms for about 2,000 vectors (est., from an exact scan of 28k vectors taking about 2 seconds [S3]). Pages newer than S's snapshot (the starter counts 469 Finnish pages missing from S [S3]) are reachable only through R2 and R3. Warning sign: fewer than k results means too few candidate pages, or a date before the page existed.

Valid and correct when. Checks 1 and 2 have 0 failures. Check 4 is within 2 percentage points of the starter on every metric, and check 5 fixes at least 2 of the 3 misses (otherwise document router recall as the bottleneck). Check 8 is 20 of 20, temporal state-recall@5 is at least 80% (from F15), and the budgets are met on the demo laptop. If checks 1 and 2 fail, fix the loader (F6), not the query. If router recall is the bottleneck, widen routing before touching the model.

### F8. Grounded answers with citation verification (Step 6, backend and LLM, gate H20)

Does. Builds an answer from numbered evidence with a citation on every sentence. It checks each quote programmatically, refuses when evidence is thin, matches the question's language, and links each citation to `?oldid=<rev_id>`.

Verifies.
1. Format (invariant): the JSON parses (one retry), every sentence has at least one citation, and every `[n]` exists.
2. Quote check (invariant), using `quote_ok` from Step 6. Report the first-pass rate. Anything failing is regenerated or dropped, so the displayed rate is 100%.
3. Faithfulness (golden set): 30 answers are judged claim by claim by a second LLM, and 15 are re-checked by a human. Judge and human agree on at least 90%.
4. Refusal: 10 unrelated questions (a sports result, say) return "not covered" with zero citations (10 of 10).
5. No future facts (invariant): all cited evidence has valid_from <= t < valid_to. For 10 questions whose answer exists only in later revisions, asked at an earlier date, the answer is a refusal or the older state (10 of 10).
6. Language: fi, sv, en, fr and ar, 2 questions each, give answers in the question's language (10 of 10), with Finnish quotes verbatim and a separate translation field.
7. Permalinks: for 20 citations, `?oldid=` resolves, its timestamp matches `valid_from` (within the 1 microsecond bump), and the quote appears in that revision's cleaned wikitext via the revisions API (20 of 20).
8. Determinism: at temperature 0, the cited set is identical across two runs for at least 90% of questions.

Expect. 3 to 6 sentences with 2 to 6 citations. The first-pass quote rate is high but not perfect, and translated quotes are the usual failure, hence verbatim Finnish plus a separate translation. First token in 2.5 s or less and the full answer in 8 s or less (est.; depends on the LLM).

Valid and correct when. Displayed citations are 100% verified (first pass at least 95%), faithfulness is at least 95%, and checks 4 to 7 all pass.

### F9. Then versus now (Step 6, backend, gate H20)

Does. For two dates t1 and t2, labels each claim behind the question as unchanged, edited (minor or major), removed, added or revived. Only the material delta goes to the LLM. The UI says "dataset end (1 Oct 2026)" for the late date.

Verifies.
1. Partition (invariant): every claim alive at t1 or t2 in the routed pages gets exactly one label.
2. Oracle from the event log. Unchanged means the same hash both times. Edited means the same claim_id with a different hash. Removed means alive at t1 only. Added means alive at t2 only and first seen after t1. Revived means alive at t2 only and seen before t1. On 30 sampled triples, agreement is 100%.
3. Symmetry and identity (invariant): swapping t1 and t2 swaps added with removed and keeps the rest. A delta of (t, t) is all unchanged.
4. Materiality (golden set): 20 typo edits are labelled minor and 20 substantive edits (a number, name or date changed) are labelled major, with at least 90% correct.
5. Narrative: every change stated corresponds to a delta item (LLM judge plus 10 human checks, at least 90%), and none is invented.

Expect. Across a 15-year gap on a busy article, most claims are edited or added. The narrative must pick the top 3 to 5 by materiality and relevance to the question. Warning sign: many "edited" items with similarity near 1.0 means a normalisation mismatch.

Valid and correct when. Checks 1 and 3 have 0 violations, check 2 is 100%, and checks 4 and 5 are at least 90%.

## C. Insight layer

### F10. Contest score (Step 7, ML, gate H24)

Does. Per claim, computes flips (overall and between named editors only), alternation between two named editors, revivals, named reverts, churn and uncited stretch. It combines the four contest signals (named flips, alternation, revivals, named reverts) by rank-average within the article into [0,1]. It keeps the grey (anonymous cleanup) versus red (named versus named) split, and it tags bulk revisions and excludes them from churn and flips.

Verifies.
1. Synthetic unit tests (invariant):

| History (states / editors) | Expected |
|---|---|
| A,B,A / u1,u2,u1 | flips 1, named flips 1, alternation 3 |
| A,B,A,B,A / u1,u2,u1,u2,u1 | flips 3, alternation 5 |
| A,V,A / u1,ip,u2 | flips 1, named flips 0 (cleanup), named reverts 0 |
| a single edit | all signals 0 |
| deleted, then restored | revivals 1 |
| 40 claims rewritten in one revision, similarity at least 0.9 | tagged bulk, no signal change |

   The flips column for the first two rows was executed in SQL on 10.11 (3 and 1).
2. Oracle by reference code: the SQL window-function version equals a Python reference on 5 pages for every integer signal.
3. Consistency with F2: at least 95% of `is_revert` revisions on hero pages touch at least one claim. List the exceptions and confirm each changes only content the chunker drops (infobox, categories), spot-checking 10.
4. Bounds and monotonicity (invariant): the score is in [0,1], and adding a flip to a history never lowers it (property test on random histories).
5. Golden set: 30 claims (10 top, 10 middle, 10 bottom, blind) labelled "fought over by editors?" by two people. At least 7 of the top 10 are yes and at most 1 of the bottom 10.
6. Stability: plus or minus 20% on the weights changes the top 10 by at most 3 items.
7. Temporary accounts (`~2026-...`) are never counted as named in alternation. The starter found 70 of them in its 50 articles [S2].
8. Localisation (reported, not gated): the share of named reverts in the starter's edit-war months (at least 3 named reverts) that touch the article's top 3 claims. If it is a majority, fights are paragraph-specific. If not, say they are diffuse.

Expect. Most claims score near 0, a tail of a few percent scores high, and zombie claims cluster in the most contested sections. Warning sign: top scores concentrated in the lead means bulk-edit pollution.

Valid and correct when. Checks 1 to 4 and 7 pass at 100%, check 5 meets its split, and check 6 is at most 3.

### F11. X-ray, claim biography, fault lines (Step 7, backend and frontend, gate H24)

Does. For one article, draws a claim by month grid of edit intensity (grey or red per the split) and lists the top fault lines. A claim biography shows states, editors, comments, diffs and permalinks.

Verifies.
1. Cell identity (invariant): the total over all cells equals the number of `claim` rows whose row_start falls in the window. 200 random cells equal a direct `GROUP BY` over `claim FOR SYSTEM_TIME ALL`.
2. Order (invariant): rows follow the reading order of the latest state, which equals the chunker's order for the latest revision (100%). Removed claims sit in a labelled band.
3. Oracle: for 10 months the starter flags as edit wars, at least 9 show at least one red cell.
4. Biography (invariant): the state count equals `n_states`, and applying each diff to the previous text reproduces the next (100%). The `oldid` links of 10 states resolve and match.
5. Privacy lint (invariant): no IPv4 or IPv6 string appears in any API response (regex). Anonymous editors show as "anonymous" and temporary accounts as "temporary account".
6. Performance: a 3,357-version article renders in 1.0 s or less on the demo laptop, and the API takes 200 ms or less (est., against the starter's 42 ms for Talvisota's whole timeline page [S2]).
7. Accessibility: red cells also carry a hatch pattern (not colour alone), and the heatmap has a text summary for screen readers.

Expect. Vertical bands for busy months, a few hot rows, and pale elsewhere, with red concentrated in the sections the dispute summaries describe. Warning sign: a uniformly red map means the grey/red classification is broken.

Valid and correct when. Checks 1, 2, 4 and 5 show 0 violations, check 3 is at least 9 of 10, and check 6 is within budget.

### F12. Dispute summaries (Step 7, backend and LLM, gate H26)

Does. For each of the top ten contested claims per article, one LLM call over the distinct states, dates and edit summaries returns `{topic, side_a, side_b, kind}` in neutral wording.

Verifies.
1. Schema (invariant): 100% valid, and `kind` is in {factual, wording, naming, sourcing, cleanup}.
2. Grounding (invariant): at least 95% of numbers, dates and capitalised tokens in a summary occur in the input, and no number is invented.
3. Neutrality lint: 0 hits for motive or politics words about editors (biased, propaganda, agenda, nationalist and similar) in 50 summaries. Ten human reads are 10 of 10 about content, not people.
4. Usefulness (golden set): 10 summaries rated 1 to 5 by two people, with mean at least 4 and no 1s.
5. Determinism: at temperature 0 a second run is identical and served from cache.

Expect. One or two sentences per side, with "cleanup" for vandalism. Warning sign: any sentence explaining why an editor did something.

Valid and correct when. Checks 1, 2 and 5 pass, the lint is clean, and check 4 is met. A summary is shown only above the contest threshold.

### F13. Audit and vintage (Step 8, backend and ML, gate H32)

Does. Takes a bare-LLM answer or pasted text and extracts at most 12 atomic claims. For each it retrieves evidence `AS OF` the chosen date, labels it supported, contradicted or absent with a verified quote, and plots the best cosine at each year-end 2005 to 2026.

Verifies.
1. Extraction (golden set): on 10 answers, at least 90% of facts are captured, each claim is atomic, and none is invented.
2. Planted set of 30 claims: 10 paraphrases of real claims (supported), 10 with a changed date, name or number (contradicted), and 10 facts you verified occur in no claim (absent). Accuracy is at least 85%, and at most 1 of the 20 non-supported claims is labelled supported (false support is the unforgivable error).
3. Temporal flip: for 10 claims true in 2010 and changed later, the audit at 2010 says supported and at dataset end says contradicted or absent (at least 8 of 10).
4. Every supported or contradicted verdict shows a quote that passes `quote_ok` (100%).
5. Oracle for vintage from claim lifetimes: for 20 states living at least 2 years, the argmax year falls inside the lifetime for at least 90%. The curve value at year-end y equals the maximum cosine among claims alive at y (exact SQL cross-check).
6. Consistency: two runs give the same verdicts for at least 95%.
7. Latency: 15 s or less for 12 claims in parallel (est.).

Expect. In a typical bare answer most claims are supported, a few are absent, and contradictions are rare. "Absent" is not "false", and the UI must say so. Label the strip "closest year", since it measures similarity, not entailment. Warning sign: everything "supported" means a retrieval date leak or a lenient judge.

Valid and correct when. Check 2 meets accuracy and false support, check 3 is at least 8 of 10, checks 4 and 5 pass, check 1 is at least 90% and check 6 is at least 95%.

## D. Product, proof, hardening

### F14. Web UI (Step 9, frontend, gate H38)

Does. Ask, Travel, X-ray and Audit views, plus permalinks, an SQL drawer with timings, and error states.

Verifies.
1. Contract (invariant): every real API response validates against its JSON schema (100%).
2. Demo path: the 7-step script passes 3 times in a row on the demo laptop, including offline replay.
3. Race (invariant): drag the slider through 20 positions quickly. Only the last response renders, and each equals a direct API call.
4. State in the URL: a reload restores question, date, tab and selected claim.
5. Labels: "now" reads "dataset end (1 Oct 2026)", and the SQL drawer shows the real statement.
6. Budgets (est.): first meaningful paint 1.5 s or less, first answer token 2.5 s or less, X-ray 1.0 s or less.
7. Failure: killing the API, the LLM and the DB in turn gives readable errors, never a blank page.
8. Accessibility: keyboard-operable slider and chips, contrast at least 4.5:1.
9. Zero console errors on the demo path.

Expect. No layout shift and no stale answers when scrubbing.

Valid and correct when. All nine hold.

### F15. Evaluation harness and benchmark sheet (Step 10, data/DB, gate H36)

Does. `make bench` regenerates every number on the benchmark sheet: retrieval on the 37 questions, the 40-question temporal set, index recall, latency, storage and scale.

Verifies.
1. Oracle for baseline fidelity: your harness reproduces the starter's article-vector numbers within 3 percentage points, and its keyword baseline of 11/16/41% [S3].
2. Reproducibility: two runs give identical quality metrics and latency within 20%. Seeds, versions, `M`, `ef_search` and hardware are recorded.
3. Temporal-set audit (golden set): each question is stored as (question, t, gold claim hash). A spot-check of 40 finds at least 90% valid, meaning the answer is present in the target state, differs from the other state, and is unambiguous.
4. Honest statistics: report x of n and which questions flipped, and draw no conclusion from differences under about 5 percentage points.
5. Index and cache experiments: exact via `IGNORE INDEX`, ef at 20, 80 and 160, and a `mhnsw_max_cache_size` sweep, since the starter left it at its default and its effect unverified [S3].

Expect. Flat or better on the old questions and clear gains on the president questions and the temporal set. That is a hypothesis until measured.

Valid and correct when. Check 1 passes, check 2 reproduces, check 3 is at least 90%, and `make bench` regenerates `docs/benchmarks.md` from a clean checkout in under 30 minutes on the hero set (est.).

### F16. Stretch: overlay and gap map (Step 11, frontend and ML, only after G3)

Does. The overlay is a userscript that sends the page title and visible paragraphs. The server embeds them, matches them to current claims, and the script paints a gutter by contest score. The gap map shows fi paragraphs with no counterpart in sv or en for topics present in all three languages.

Verifies.
1. Overlay match accuracy: on 3 hero pages by 10 paragraphs, at least 90% match the correct claim and at most 5% are painted wrongly. Unmatched paragraphs (cosine below 0.85 or ambiguous) stay unpainted.
2. Privacy: the network log shows only title and paragraph text, with no identifiers, and there is an off switch.
3. Robustness: 5 pages with tables, infoboxes and refs do not break. A non-hero page shows "no history data", and overhead is 300 ms or less.
4. Parity: tooltip values equal the X-ray's.
5. Gap map: 30 paragraph judgements agree at least 80% with the machine's "has counterpart" call, and every flagged gap shows its nearest counterpart and score.

Expect. Gutter colours matching the X-ray rows.

Valid and correct when. Checks 1 to 4 pass for the overlay and check 5 is at least 80% for the gap map.

### F17. Demo hardening (Step 12, everyone, gate H42)

Does. Replay mode, an `llm_cache` table, one-command setup, the backup video and the README.

Verifies.
1. Cache: the hit rate on demo prompts is 100%, and with Wi-Fi off the outputs are identical to the live run.
2. Warm-up: the first answer after a cold start takes 5 s or less. The starter's first search took about 1.6 s while Ollama loaded the model [S3], so pre-warm.
3. Fresh clone: `make setup && make demo` works on a clean machine in 30 minutes or less with datasets pre-downloaded (est.).
4. Repo hygiene: a secret scan is clean, and the README credits Wikipedia under CC BY-SA 4.0 [S1] and links permalinks.
5. Three timed rehearsals come in at 3:00 or less without intervention.

Expect. The whole demo runs with no network.

Valid and correct when. All five pass and the backup video plays.

---

## 8. Gates, cut lines, team shapes

| Gate | Must be demoable | If missed |
|---|---|---|
| G1, H14 | Time-travel answer with verified citations on 10 pages | Drop then/now delta and hybrid |
| G2, H24 | X-ray with dispute summaries on 50 pages | Drop audit and all stretch; polish the X-ray |
| G3, H32 | Audit and benchmark table filled | Ship without vintage; benchmarks come first |
| H36 | Feature freeze | Fix only P0 bugs |
| H42 | Submission | Two hours of slack before the deadline |

Roles for 4: data/DB (loaders, schema, benchmarks), ML (chunker, resolver, embeddings, contest score, eval sets), backend/LLM (API, retrieval SQL, prompts, audit), frontend/story (UI, overlay, demo, slides). For 3, merge backend into data and ML. For 2, cut audit and the stretch items. For solo, ship Ask plus Travel plus one hero X-ray.

## 9. What the starters left open, and the number on the slide

| Left open | What you ship | Number on the slide |
|---|---|---|
| Ask step not built | Grounded answers, quote verification, permalinks | % of citations verified verbatim (target at least 95% first pass) |
| Facts inside long biographies missed | Chunk-level retrieval over claims | hit@k and MRR before and after on the 37 questions; the 5 president questions |
| Hybrid search | RRF of vector plus FULLTEXT prefix queries | MRR delta |
| RAG plus SVT combination | Route, `AS OF`, exact rank in one statement | Temporal state-recall@5 vs current-only; p95 latency |
| Full copies do not scale | Claim-level SVT plus content-addressed chunks | MB and ratio on the same 50 pages; round-trip pass rate |
| Parquet loader, all articles | `page_state` for 862k versions | Load time, size, triage latency |
| Cleanup vs real edit wars | Claim-level contest score with named alternation | precision@10 on 30 hand labels |
| Filtered-ANN and cache questions | Oversampling path vs exact; cache sweep | recall@10, p50 and p95 |
| More test questions wanted | Published temporal set plus `make bench` | The CSVs |
| Where should it live? | Userscript overlay on the API | Live demo on fi.wikipedia |

MariaDB features exercised: system versioning with real times, window functions, recursive CTE for S's category scoping, HNSW plus FULLTEXT hybrid, JSON layout, and cross-schema CTEs.

## 10. Demo script (3 minutes)

1. Ask in English: "Why did the Finnish Civil War start?" You get a cited answer, and hovering shows the Finnish paragraph with a verified tick.
2. Drag the slider to 2008. The answer changes and the "what changed" panel lists the edited and added claims.
3. Ask the question the starter misses: "Who was president of Finland from 1956 to 1982?" The starter misses Kekkonen with whole-article vectors [S3], and chunk-level retrieval returns the right paragraph.
4. X-ray the Civil War article. Click a red band to see its dispute summary and zombie badge, and point out that grey bands are anonymous cleanup, not war.
5. Audit a bare-LLM answer: verdict chips and the vintage strip.
6. Open the live fi.wikipedia page with the overlay on, if built.
7. Numbers slide, and one closing line: retrieval, history and audit run in one MariaDB, and one statement does route, `AS OF`, rank.

## 11. Risks, pitfalls, mentor questions

### Risks

| Risk | Mitigation |
|---|---|
| Lineage bugs | The round-trip test is the gate; exact diff first, semantic merge last |
| Embedding compute | Count unique hashes at H8, typo shortcut, GPU by H6 |
| Versioned-table vector quirks | Exact routed path is the default; ANN path is optional and measured |
| Cleanup reverts read as edit wars | Anonymous vs named split, alternation signal, hand-labelled precision |
| LLM latency or Wi-Fi | Streaming, `llm_cache`, replay mode |
| Noisy Finnish wikitext | Reuse the starter's cleaner, eyeball 30 chunks |
| Ethics | Show editors as public handles (temporary accounts as "temporary account"), never rank people; the tool is about claims. Attribute via permalinks. |
| Page-blanking vandalism inflates the claim store | Optional collapse of undone anonymous bulk deletions; measure row-versions per revision |
| Scope creep | One hero article perfect first; freeze at H36 |

### Pitfalls (starters and sandbox)

- Server-side prepared statements: the starter reports that `TIMESTAMP ?` refused a bare date [S2]. In the sandbox an SQL-level PREPARE with a string variable accepted one on 10.11, so this depends on the client path. Always pass a full `'YYYY-MM-DD HH:MM:SS[.ffffff]'`.
- System-versioned tables cannot be TEMPORARY (error 4137 in the sandbox, as the starter says [S2]). Use real staging tables and drop them.
- `ALTER TABLE` that adds or changes a column on a versioned table fails (error 4119) unless `SET system_versioning_alter_history = KEEP`; with KEEP it works and old rows read NULL. Adding an index worked without it. Freeze the schema by H6.
- Same-second edits: a zero-length history row is rejected (error 1525). Apply the 1 microsecond bump to every table consistently.
- An `UPDATE` on a versioned row writes a history row stamped with the real clock. Compute flags at load time.
- `LIMIT` directly inside `IN (SELECT ...)` is unsupported (error 1235). Wrap it in a derived table.
- A table alias goes after the `FOR SYSTEM_TIME` clause.
- Batch inserts by bytes; the default packet is 16 MB [S2].
- A flat join defeats the vector index; put `ORDER BY distance LIMIT` in a subquery and join afterwards. Set `mhnsw_ef_search` per session [S3].
- HNSW quality depends on insertion order [S3]; re-measure recall after the final load.
- Ollama needs both `num_ctx` and `num_batch` [S3].
- Use UTC in every connection (`SET time_zone = '+00:00'`) [S2].
- All of the above was observed on MariaDB 10.11. Re-run Appendix C on the real server (12.3.x) before relying on any of it.

### Questions for the mentors (first hour)

1. Is a vector index on a system-versioned table meant to work with `AS OF` (the MDEV-35246 behaviour [S6])?
2. Does Toolforge's ToolsDB have vector support, for the path-to-production slide?
3. Is `mhnsw_max_cache_size` the right knob at 500k or more vectors?
4. Is any GPU or compute available for the embedding step?

## 12. Validation dashboard (copy into `docs/validation.md`)

| Feature | Pass condition | Gate |
|---|---|---|
| F1 | Starter baseline reproduced within 3 pp of 57/76/86% | H2 |
| F2 | 862,076 versions, 19,742 pages, 0 broken chains, starter's 31,848/3,357/2,143/328 matched | H5 |
| F3 | Median at most 2 and P90 at most 6 changed hashes per edit; N recorded | H8 |
| F4 | 9 of 9 unit cases, precision at least 95%, recall at least 80%, vandalism oracle 20 of 20 | H12 |
| F5 | 0 orphans, 200 of 200 self-retrieval, ANN recall at least 95% | H14 |
| F6 | Round trip 100% in all three variants | H14 |
| F7 | 0 time-filter violations, 100 of 100 appear/disappear, non-inferior to the starter, at least 2 of 3 president misses fixed | H14 |
| F8 | Displayed citations 100% verified, faithfulness at least 95%, refusal 10 of 10 | H20 |
| F9 | Partition and symmetry hold, labels 100% on the event log | H20 |
| F10 | Unit tests and cross-implementation 100%, at least 7 of 10 hand-labelled | H24 |
| F11 | Cell identity exact, at least 9 of 10 starter war months red, no IP strings | H24 |
| F12 | Schema 100%, grounding at least 95%, neutrality lint clean, usefulness mean at least 4 | H26 |
| F13 | Verdict accuracy at least 85%, false support at most 1 of 20, vintage at least 90% | H32 |
| F15 | `make bench` reproduces from a clean checkout | H36 |
| F14, F17 | Demo path 3 of 3 in replay mode | H38 to H42 |
| F16 (stretch) | Overlay match at least 90%, at most 5% mispainted | after G3 |

The project is done when every line is green or documented as a limitation, the demo path passes 3 times in a row offline, and the README quotes only numbers that `make bench` produced.

---

## 13. What was verified in a sandbox

Environment: Ubuntu 24.04, Python 3.12.3 (numpy, scipy, rapidfuzz 3.14.6, pymysql, xxhash), MariaDB 10.11.14. The target server is `mariadb:lts` (12.3.x), so every database result below must be re-checked there (Appendix C).

| What | How | Result |
|---|---|---|
| Lineage resolver, 9 unit cases (insert, delete, typo, move, rewrite, split, merge, blank and restore, unrelated swap) | `test_resolver.py` | 8 pass as specified. The rewrite case gave delete plus add at pass 1 (similarity 0.57 against the 0.60 threshold), as the plan predicts. Replay is deterministic. |
| Resolver simulation, 400 random edits (typo, light rewrite, move, insert, delete) over 30 paragraphs | ground-truth ids | 0 id mismatches in 13,366 paragraph-revision pairs |
| Backdated history rows as an ordinary user; `AS OF` boundaries | `pitfalls.sql` T1 | Pass. row_start inclusive, row_end exclusive, 1 microsecond earlier returns the previous state. |
| Claim deletion and revival with the same primary key | `pitfalls.sql` LIFE | Pass. `AS OF` inside the gap returns nothing for that claim; 0 keys with more than one current row. |
| Same-second edits | `pitfalls.sql` and `e2e.py` | A zero-length history row is rejected (error 1525), so the bump is mandatory. 357 of 1,242 synthetic revisions needed it. |
| End-to-end toy: 1,242 synthetic revisions (edits, moves, inserts, deletes, 41 blank-and-restore episodes) through the resolver into `page_state`, `claim`, `layout`, then reconstruction | `e2e.py` | Round trip 1,242 of 1,242 at revision times, 1,241 of 1,241 at 1 microsecond before, 1,000 of 1,000 at random instants. 143 distinct claims; 2,984 claim row-versions (2.40 per revision); 374 layout row-versions. Chain breaks 0; overlapping claim periods 0; keys with more than one current row 0. Reconstruction p50 0.9 ms, p95 1.45 ms (toy table). |
| `is_revert` and `undone` flags | `revert_test2.py` | 5 golden cases pass. Python `is_revert` equals the starter-style window query (81 reverts on the toy history). `undone` flags exactly the 41 blanking revisions, 0 good revisions wrongly flagged. |
| Date snapping past vandalism | `snap_test.py` | 1,000 of 1,000 snaps equal the last surviving revision. In the 41 instants inside a vandalised window, plain `AS OF` returned the vandalised state every time and the snapped query returned the pre-vandalism state every time. |
| Headline query structure (CTE chain, two schemas, `AS OF` in a CTE, derived-table `LIMIT`, UNION, FULLTEXT prefix) | `routing_shape.sql`, with numeric stand-ins for vector distances | Parses and returns the expected rows. The prefix term `mannerheim*` matched both the bare and the inflected form (2 hits against 1 and 1). |
| Window-function flips and chain check on `FOR SYSTEM_TIME ALL` | `pitfalls.sql` | A,B,A,B,A gives 3 flips, A,B,A gives 1; chain check returns 0 on a valid chain. |
| Pitfalls | `pitfalls.sql` | Temporary versioned tables rejected (4137); ADD COLUMN needs `system_versioning_alter_history=KEEP` (4119), ADD KEY does not; `LIMIT` inside `IN` rejected (1235); alias goes after the `FOR SYSTEM_TIME` clause; an UPDATE writes a history row stamped with the real clock. |

Not verified (all remain open until you run them on the real stack):
- The `VECTOR` type, `VECTOR INDEX`, `VEC_DISTANCE_COSINE`, `VEC_FromText`, `mhnsw_ef_search`, and smoke tests T2 and T3. These need MariaDB 11.7 or later.
- Anything on the real data: counts, the starter's 328 reverts, chunk and embedding counts, storage, latency, recall. Every "est." in this document.
- bge-m3 throughput, Ollama versus sentence-transformers parity, prompts, LLM behaviour, the UI, the userscript.
- Behaviour on MariaDB 12.3.x for everything in the pitfalls list.

To re-run the tests: save the Step 3 function as `resolver.py` and the files in the appendices under the names shown, install `numpy scipy rapidfuzz pymysql xxhash`, create a database `t` and a user `u` (password `u`) with a unix socket, then run `python3 test_resolver.py`, `python3 e2e.py`, `python3 revert_test2.py`, `python3 snap_test.py` and `mariadb -u u -pu t --force --table < pitfalls.sql`.

---

## Appendix A. Resolver and its tests

`resolver.py` (the function from Step 3, as executed):

```python
from difflib import SequenceMatcher
from rapidfuzz.fuzz import ratio
from scipy.optimize import linear_sum_assignment
import numpy as np

def resolve(prev, cur, grave, new_id, t_edit=0.60):
    """prev: [(claim_id, hash, text)] of rev i-1; cur: [(hash, text)] of rev i; grave: {hash: claim_id}."""
    ids, events = [None] * len(cur), []
    sm = SequenceMatcher(None, [p[1] for p in prev], [c[0] for c in cur], autojunk=False)
    gone, fresh = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ids[j1:j2] = [p[0] for p in prev[i1:i2]]
        else:
            gone += range(i1, i2); fresh += range(j1, j2)
    if gone and fresh:                                   # rewritten in place or moved
        S = np.array([[ratio(prev[o][2], cur[n][1]) / 100 for n in fresh] for o in gone])
        for a, b in zip(*linear_sum_assignment(-S)):
            if S[a, b] >= t_edit:
                ids[fresh[b]] = prev[gone[a]][0]; events.append(("edit", ids[fresh[b]], S[a, b]))
    used = {i for i in ids if i is not None}
    for o in gone:                                       # no successor: deleted
        if prev[o][0] not in used:
            events.append(("delete", prev[o][0], None)); grave[prev[o][1]] = prev[o][0]
    for n in fresh:                                      # no predecessor: revival or brand new
        if ids[n] is None:
            h = cur[n][0]
            if h in grave and grave[h] not in used:
                ids[n] = grave.pop(h); events.append(("revive", ids[n], None))
            else:
                ids[n] = new_id(); events.append(("add", ids[n], None))
            used.add(ids[n])
    return ids, events
```

`test_resolver.py` (nine unit cases, determinism, and a 400-revision simulation with ground truth):

```python
import re, unicodedata, random, xxhash
from resolver import resolve
from rapidfuzz.fuzz import ratio

def norm(s): return re.sub(r"\W+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()
def H(t): return xxhash.xxh64_intdigest(norm(t).encode("utf-8"))
assert 0 <= H("x") < 2**64

class Run:
    def __init__(self): self.n = 0; self.grave = {}; self.cur = []; self.log = []
    def nid(self): self.n += 1; return self.n
    def step(self, paras):
        cur = [(H(p), p) for p in paras]
        ids, ev = resolve(self.cur, cur, self.grave, self.nid)
        assert len(ids) == len(cur) and len(set(ids)) == len(ids), "id invariants"
        self.cur = [(ids[k], cur[k][0], cur[k][1]) for k in range(len(cur))]
        self.log.append((ids, ev)); return ids, ev

P = [
 "The Finnish Civil War was fought in 1918 between the Reds and the Whites after Finland declared independence from Russia in December 1917.",
 "The Winter War began in November 1939 when the Soviet Union attacked Finland and ended with the Moscow Peace Treaty in March 1940.",
 "The Continuation War lasted from 1941 to 1944 and ended with an armistice signed in Moscow on 19 September 1944 between Finland and the Allies.",
 "Urho Kekkonen served as President of Finland from 1956 to 1982 and shaped the country's foreign policy during the Cold War era.",
 "Finland joined the European Union in 1995 and adopted the euro as its currency in 1999 together with ten other member states.",
 "The Aland Islands are an autonomous region of Finland with a Swedish-speaking population and a special status under international law.",
]
kinds = lambda ev: sorted(e[0] for e in ev)
res = {}

# 1 insert
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[1], P[3], P[2]])
assert b == [a[0], a[1], 4, a[2]] and kinds(ev) == ["add"], (a, b, ev); res["insert"] = "pass"
# 2 delete
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[2]])
assert b == [a[0], a[2]] and kinds(ev) == ["delete"] and ev[0][1] == a[1]; res["delete"] = "pass"
# 3 typo
r = Run(); a, _ = r.step(P[:3]); t = P[1].replace("November", "Novembre")
b, ev = r.step([P[0], t, P[2]])
assert b == a and kinds(ev) == ["edit"] and ev[0][2] >= 0.9, ev; res["typo"] = f"pass (sim {ev[0][2]:.2f})"
# 4 move
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[2], P[1]])
assert sorted(b) == sorted(a) and b == [a[0], a[2], a[1]] and "add" not in kinds(ev) and "delete" not in kinds(ev), (a, b, ev); res["move"] = f"pass {kinds(ev)}"
# 5 rewrite (pass 1: expect delete+add, similarity below threshold)
rew = "In late November 1939 the Soviet Union invaded Finland, which started the Winter War; it concluded in March 1940 with the Treaty of Moscow."
sim = ratio(P[1], rew) / 100
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], rew, P[2]])
res["rewrite"] = f"pass-1 sim {sim:.2f} -> {kinds(ev)} (semantic pass needed)" if sim < 0.6 else f"sim {sim:.2f} -> {kinds(ev)}"
# 6 split
cut = P[1].index(" and ended")
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[1][:cut] + ".", "It " + P[1][cut+1:], P[2]])
assert "delete" not in kinds(ev) and kinds(ev).count("add") == 1 and kinds(ev).count("edit") == 1 and b[0] == a[0] and b[3] == a[2], (a, b, ev)
res["split"] = f"pass {kinds(ev)}"
# 7 merge
r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0] + " " + P[1], P[2]])
assert kinds(ev) == ["delete", "edit"] and b[1] == a[2] and b[0] in (a[0], a[1]), (a, b, ev); res["merge"] = f"pass {kinds(ev)}"
# 8 blank and restore
r = Run(); a, _ = r.step(P); b, e1 = r.step(["Page blanked."]); c, e2 = r.step(P)
assert c == a and kinds(e2).count("revive") == len(P) and "add" not in kinds(e2) and kinds(e1).count("delete") == len(P); res["blank+restore"] = "pass (all revived, 0 new ids)"
# 9 unrelated swap
X = "Helsinki hosted the Summer Olympic Games in 1952, drawing athletes from sixty-nine countries to the new stadium."
Y = "Nokia began as a paper mill in 1865 before turning to rubber, cables and finally mobile telephones in the late twentieth century."
r = Run(); a, _ = r.step([P[0], P[1], P[2]]); b, ev = r.step([P[0], X, Y])
cross = max(ratio(P[1], X), ratio(P[1], Y), ratio(P[2], X), ratio(P[2], Y)) / 100
assert b[0] == a[0] and b[1] not in a and b[2] not in a and kinds(ev) == ["add", "add", "delete", "delete"], (b, ev); res["unrelated swap"] = f"pass (max cross sim {cross:.2f})"
# replay determinism
def replay():
    r = Run(); out = []
    for revs in ([P[:3]], [[P[0], P[2]]], [P[:3]], [[P[0], "Page blanked."]], [P[:3]]):
        out.append(r.step(revs[0])[0])
    return out
assert replay() == replay(); res["determinism"] = "pass"

# simulation vs ground truth
random.seed(7)
vocab = [w for p in P for w in re.findall(r"[A-Za-z]+", p)] + ["treaty", "army", "border", "senate", "harbour", "railway", "census", "parliament", "armistice", "referendum"]
def para(): return " ".join(random.choice(vocab) for _ in range(random.randint(18, 28))).capitalize() + "."
def typo(s):
    w = s.split(); i = random.randrange(len(w)); w[i] = w[i] + random.choice("sx"); return " ".join(w)
def light(s):  # replace ~10% of words
    w = s.split()
    for _ in range(max(1, len(w)//10)): w[random.randrange(len(w))] = random.choice(vocab)
    return " ".join(w)
doc = [(i, para()) for i in range(30)]; nextg = 30
r = Run(); ids, _ = r.step([t for _, t in doc]); gt2res = {g: ids[k] for k, (g, _) in enumerate(doc)}
bad = total = 0; ops = {"typo": 0, "light": 0, "move": 0, "insert": 0, "delete": 0}
for _ in range(400):
    op = random.choice(list(ops)); ops[op] += 1; k = random.randrange(len(doc)) if doc else 0
    if op == "typo" and doc: doc[k] = (doc[k][0], typo(doc[k][1]))
    elif op == "light" and doc: doc[k] = (doc[k][0], light(doc[k][1]))
    elif op == "move" and len(doc) > 1: x = doc.pop(k); doc.insert(random.randrange(len(doc) + 1), x)
    elif op == "insert": doc.insert(random.randrange(len(doc) + 1), (nextg, para())); nextg += 1
    elif op == "delete" and len(doc) > 3: doc.pop(k)
    ids, _ = r.step([t for _, t in doc])
    for (g, _), cid in zip(doc, ids):
        if g in gt2res:
            total += 1; bad += gt2res[g] != cid
        else:
            gt2res[g] = cid
print({k: v for k, v in res.items()}); print("ops", ops)
print(f"simulation: {total} paragraph-revision pairs, {bad} id mismatches ({100*bad/total:.3f}%)")
```

---

## Appendix B. Toy end-to-end harness

These scripts need a local MariaDB reachable on a unix socket (adapt the connection line). `e2e.py` generates a synthetic history, runs the resolver, loads `page_state`, `claim` and `layout` with real (bumped) times, and runs the round-trip checks. `revert_test2.py` and `snap_test.py` read the tables that `e2e.py` leaves behind.

`e2e.py`:

```python
import json, random, hashlib, time, re, unicodedata, xxhash
from datetime import datetime, timedelta
import pymysql
from resolver import resolve

def norm(s): return re.sub(r"\W+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()
def H(t): return xxhash.xxh64_intdigest(norm(t).encode("utf-8"))
fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f")

def history_rows(versions):            # code exactly as in the plan
    starts, last = [], None
    for v in versions:
        s = v["timestamp"] if last is None or v["timestamp"] > last else last + timedelta(microseconds=1)
        starts.append(s); last = s
    for i, v in enumerate(versions):
        yield v, starts[i], (starts[i + 1] if i + 1 < len(versions) else None)

random.seed(11)
vocab = "treaty army border senate harbour railway census parliament armistice referendum war peace Finland Soviet Sweden Russia president election republic independence Karelia Helsinki Tampere coast".split()
def para(): return " ".join(random.choice(vocab) for _ in range(random.randint(18, 28))).capitalize() + "."
def typo(s): w = s.split(); i = random.randrange(len(w)); w[i] += random.choice("sx"); return " ".join(w)
def light(s):
    w = s.split()
    for _ in range(max(1, len(w)//10)): w[random.randrange(len(w))] = random.choice(vocab)
    return " ".join(w)

# ---- synthetic revision history: edits, moves, inserts, deletes, vandalism+revert, same-second collisions
doc = [para() for _ in range(40)]
revs, t = [], datetime(2010, 1, 1, 12, 0, 0); rid = 1000
def emit(paras, editor, anon, comment):
    global rid, t
    t += timedelta(seconds=random.choice([0, 0, 1, 5, 60, 3600, 86400])); rid += 1
    txt = "\n\n".join(paras)
    revs.append(dict(rev_id=rid, timestamp=t, paras=list(paras), editor=editor, anon=anon, comment=comment,
                     sha1=hashlib.sha1(txt.encode()).hexdigest()[:31], bytes=len(txt)))
emit(doc, "Alice", 0, "create")
for _ in range(1200):
    op = random.choices(["typo", "light", "move", "insert", "delete", "vandal"], [45, 20, 8, 8, 6, 3])[0]
    k = random.randrange(len(doc)); before = list(doc)
    if op == "typo": doc[k] = typo(doc[k])
    elif op == "light": doc[k] = light(doc[k])
    elif op == "move": x = doc.pop(k); doc.insert(random.randrange(len(doc) + 1), x)
    elif op == "insert": doc.insert(random.randrange(len(doc) + 1), para())
    elif op == "delete" and len(doc) > 5: doc.pop(k)
    if op == "vandal":
        emit(["BLANKED"], "~2026-1-1", 1, "vandalism"); emit(before, "Bob", 0, "revert")
    else:
        emit(doc, random.choice(["Alice", "Bob", "Carol", "~2026-2-2"]), random.choice([0, 0, 1]), op)

# ---- lineage
class Run:
    def __init__(s): s.n = 0; s.grave = {}; s.cur = []
    def nid(s): s.n += 1; return s.n
    def step(s, paras):
        cur = [(H(p), p) for p in paras]
        ids, ev = resolve(s.cur, cur, s.grave, s.nid)
        s.cur = [(ids[k], cur[k][0], cur[k][1]) for k in range(len(cur))]
        return ids, [c[0] for c in cur]
run = Run(); ids_per, hashes_per = [], []
t0 = time.time()
for r in revs:
    ids, hs = run.step(r["paras"]); ids_per.append(ids); hashes_per.append(hs)
print(f"revisions={len(revs)} distinct claims={run.n} lineage time={time.time()-t0:.2f}s")

# ---- tables
con = pymysql.connect(unix_socket="/run/mysqld/mysqld.sock", user="u", password="u", database="t", autocommit=False)
cur = con.cursor()
cur.execute("SET time_zone='+00:00'"); cur.execute("SET system_versioning_insert_history=1")
P = """row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE, row_end TIMESTAMP(6) GENERATED ALWAYS AS ROW END INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)"""
for tb in ("page_state", "claim", "layout"): cur.execute(f"DROP TABLE IF EXISTS {tb}")
cur.execute(f"""CREATE TABLE page_state (page_id INT UNSIGNED NOT NULL, title VARCHAR(255) NOT NULL, rev_id INT UNSIGNED NOT NULL,
  editor VARCHAR(255), anon BOOL NOT NULL, minor BOOL NOT NULL, comment TEXT, sha1 CHAR(31), bytes INT UNSIGNED, {P},
  PRIMARY KEY (page_id), KEY (page_id, row_start), KEY (sha1), KEY (rev_id)) WITH SYSTEM VERSIONING""")
cur.execute(f"""CREATE TABLE claim (page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL, text_hash BIGINT UNSIGNED NOT NULL,
  rev_id INT UNSIGNED NOT NULL, {P}, PRIMARY KEY (page_id, claim_id), KEY (page_id, row_start), KEY (text_hash)) WITH SYSTEM VERSIONING""")
cur.execute(f"""CREATE TABLE layout (page_id INT UNSIGNED PRIMARY KEY, rev_id INT UNSIGNED NOT NULL, claim_order JSON NOT NULL, {P}) WITH SYSTEM VERSIONING""")

PAGE = 42
hist = [(v, s, e) for v, s, e in history_rows(revs)]
ts_b = [s for _, s, _ in hist]                                    # bumped start per revision
ps_h = [(PAGE, "Test", v["rev_id"], v["editor"], v["anon"], 0, v["comment"], v["sha1"], v["bytes"], fmt(s), fmt(e)) for v, s, e in hist if e]
ps_c = [(PAGE, "Test", v["rev_id"], v["editor"], v["anon"], 0, v["comment"], v["sha1"], v["bytes"], fmt(s)) for v, s, e in hist if not e]
cur.executemany("INSERT INTO page_state (page_id,title,rev_id,editor,anon,minor,comment,sha1,bytes,row_start,row_end) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ps_h)
cur.executemany("INSERT INTO page_state (page_id,title,rev_id,editor,anon,minor,comment,sha1,bytes,row_start) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ps_c)

open_c, ch, lh = {}, [], []; open_l = None
for i, r in enumerate(revs):
    ts = ts_b[i]; now = dict(zip(ids_per[i], hashes_per[i]))
    for cid, (h, s, rv) in list(open_c.items()):
        if cid not in now: ch.append((PAGE, cid, h, rv, fmt(s), fmt(ts))); del open_c[cid]
        elif now[cid] != h: ch.append((PAGE, cid, h, rv, fmt(s), fmt(ts))); open_c[cid] = (now[cid], ts, r["rev_id"])
    for cid, h in now.items():
        if cid not in open_c: open_c[cid] = (h, ts, r["rev_id"])
    if open_l is None: open_l = (ids_per[i], ts, r["rev_id"])
    elif open_l[0] != ids_per[i]: lh.append((PAGE, open_l[2], json.dumps(open_l[0]), fmt(open_l[1]), fmt(ts))); open_l = (ids_per[i], ts, r["rev_id"])
cur.executemany("INSERT INTO claim (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (%s,%s,%s,%s,%s,%s)", ch)
cur.executemany("INSERT INTO claim (page_id,claim_id,text_hash,rev_id,row_start) VALUES (%s,%s,%s,%s,%s)", [(PAGE, c, h, rv, fmt(s)) for c, (h, s, rv) in open_c.items()])
cur.executemany("INSERT INTO layout (page_id,rev_id,claim_order,row_start,row_end) VALUES (%s,%s,%s,%s,%s)", lh)
cur.execute("INSERT INTO layout (page_id,rev_id,claim_order,row_start) VALUES (%s,%s,%s,%s)", (PAGE, open_l[2], json.dumps(open_l[0]), fmt(open_l[1])))
con.commit()
cur.execute("SELECT COUNT(*) FROM claim FOR SYSTEM_TIME ALL"); n_claim_rows = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM layout FOR SYSTEM_TIME ALL"); n_layout_rows = cur.fetchone()[0]
print(f"claim row-versions={n_claim_rows} ({n_claim_rows/len(revs):.2f} per revision), layout row-versions={n_layout_rows}")

def reconstruct(ts):
    cur.execute("SELECT claim_order FROM layout FOR SYSTEM_TIME AS OF TIMESTAMP %s WHERE page_id=%s", (ts, PAGE)); row = cur.fetchone()
    if not row: return None
    order = json.loads(row[0])
    cur.execute("SELECT claim_id, text_hash FROM claim FOR SYSTEM_TIME AS OF TIMESTAMP %s WHERE page_id=%s", (ts, PAGE)); h = dict(cur.fetchall())
    if set(h) != set(order): return ("SET MISMATCH", sorted(set(h) ^ set(order))[:5])
    return [h[c] for c in order]

# (a) every revision at its bumped timestamp, (b) 1 us before it, (c) random instants
bad_a = sum(reconstruct(fmt(ts_b[i])) != hashes_per[i] for i in range(len(revs)))
bad_b = sum(reconstruct(fmt(ts_b[i] - timedelta(microseconds=1))) != hashes_per[i-1] for i in range(1, len(revs)))
import bisect
bad_c = 0; lat = []
for _ in range(1000):
    x = ts_b[0] + (ts_b[-1] - ts_b[0]) * random.random(); j = bisect.bisect_right(ts_b, x) - 1
    q0 = time.time(); got = reconstruct(fmt(x)); lat.append((time.time() - q0) * 1000); bad_c += got != hashes_per[j]
lat.sort()
print(f"ROUND TRIP: at revision times {len(revs)-bad_a}/{len(revs)}; 1us before {len(revs)-1-bad_b}/{len(revs)-1}; random instants {1000-bad_c}/1000")
print(f"reconstruct latency (2 queries, toy table): p50={lat[500]:.2f} ms p95={lat[950]:.2f} ms")
cur.execute("""SELECT COUNT(*) FROM (SELECT row_start, row_end, LEAD(row_start) OVER (ORDER BY row_start) ns FROM page_state FOR SYSTEM_TIME ALL WHERE page_id=%s) h
               WHERE row_end <= row_start OR (ns IS NOT NULL AND row_end <> ns)""", (PAGE,)); print("page_state chain broken:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM page_state FOR SYSTEM_TIME ALL"); print("page_state versions:", cur.fetchone()[0], "expected", len(revs))
cur.execute("SELECT COUNT(*) - COUNT(DISTINCT page_id) FROM page_state"); print("page_state current rows - distinct pages:", cur.fetchone()[0])
cur.execute("""SELECT COUNT(*) FROM (SELECT claim_id, row_start, row_end, LEAD(row_start) OVER (PARTITION BY claim_id ORDER BY row_start) ns FROM claim FOR SYSTEM_TIME ALL) h
               WHERE row_end <= row_start OR (ns IS NOT NULL AND row_end > ns)"""); print("claim overlapping/invalid periods:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM (SELECT page_id, claim_id FROM claim GROUP BY page_id, claim_id HAVING COUNT(*)>1) x"); print("claim PKs with >1 current row:", cur.fetchone()[0])
n_same = sum(1 for v, s, e in hist if v["timestamp"] != s); print("revisions that needed the 1us bump:", n_same)
vand = [i for i, r in enumerate(revs) if r["comment"] == "vandalism"]
ok = all(ids_per[i+1] == ids_per[i-1] for i in vand if i > 0 and i+1 < len(revs)); print(f"vandalism episodes={len(vand)}; ids after revert == ids before vandalism: {ok}")
```

`revert_test2.py` (golden cases for `is_revert` and `undone`, then the toy history):

```python
import pymysql
from datetime import timedelta

def flags(sha1s, times, max_span=timedelta(hours=48)):
    """is_revert[i]: sha1 equals an earlier version's but not the previous one's (the starter's rule).
       undone[j]: j sits between the closest earlier identical version a and the reverting version b,
       unless a was itself undone (repeat of vandalism) or the span is longer than max_span."""
    n = len(sha1s); is_rev = [False]*n; undone = [False]*n; last_seen = {}
    for i, s in enumerate(sha1s):
        if s in last_seen and (i == 0 or s != sha1s[i-1]):
            is_rev[i] = True
            a = last_seen[s]
            if not undone[a] and times[i] - times[a] <= max_span:
                for j in range(a + 1, i): undone[j] = True
        last_seen[s] = i
    return is_rev, undone

T = lambda *h: [timedelta(hours=x) for x in h]
assert flags(list("ABCA"), T(0,1,2,3)) == ([False,False,False,True], [False,True,True,False])                 # basic revert
assert flags(list("AVAVA"), T(0,1,2,3,4))[1] == [False,True,False,True,False]                                  # repeated vandalism: no good edit flagged
assert flags(list("AVAXV"), T(0,1,2,3,4))[1] == [False,True,False,False,False]                                 # blank repeated after a good edit X: X survives
assert flags(list("ABCA"), T(0,10,20,100))[1] == [False]*4                                                      # span > 48 h: not cleanup
assert flags(list("AABB"), T(0,1,2,3)) == ([False]*4, [False]*4)                                               # consecutive duplicates
print("golden cases ok")
con = pymysql.connect(unix_socket="/run/mysqld/mysqld.sock", user="u", password="u", database="t"); c = con.cursor()
c.execute("SELECT sha1, comment, row_start FROM page_state FOR SYSTEM_TIME ALL WHERE page_id=42 ORDER BY row_start")
rows = c.fetchall(); rev, und = flags([r[0] for r in rows], [r[2] for r in rows])
vand = {i for i, r in enumerate(rows) if r[1] == "vandalism"}
und_idx = {i for i, u in enumerate(und) if u}
print(f"undone={len(und_idx)} blank revisions={len(vand)} exact match: {und_idx == vand}; good revisions wrongly flagged: {len(und_idx - vand)}")
```

`snap_test.py` (date snapping past vandalism):

```python
import io, contextlib, bisect, random
from datetime import timedelta
with contextlib.redirect_stdout(io.StringIO()):
    import e2e
from e2e import revs, ts_b, hashes_per, reconstruct, cur, fmt, con

def flags(sha1s, times, max_span=timedelta(hours=48)):
    n = len(sha1s); is_rev = [False]*n; undone = [False]*n; last_seen = {}
    for i, s in enumerate(sha1s):
        if s in last_seen and (i == 0 or s != sha1s[i-1]):
            is_rev[i] = True; a = last_seen[s]
            if not undone[a] and times[i] - times[a] <= max_span:
                for j in range(a + 1, i): undone[j] = True
        last_seen[s] = i
    return is_rev, undone
und = flags([r["sha1"] for r in revs], ts_b)[1]
cur.execute("DROP TABLE IF EXISTS ps2")
cur.execute("""CREATE TABLE ps2 (page_id INT UNSIGNED NOT NULL, rev_id INT UNSIGNED NOT NULL, undone BOOL NOT NULL,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE, row_end TIMESTAMP(6) GENERATED ALWAYS AS ROW END INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start,row_end), PRIMARY KEY (page_id), KEY (page_id,row_start)) WITH SYSTEM VERSIONING""")
cur.executemany("INSERT INTO ps2 (page_id,rev_id,undone,row_start,row_end) VALUES (%s,%s,%s,%s,%s)", [(42, r["rev_id"], und[i], fmt(ts_b[i]), fmt(ts_b[i+1])) for i, r in enumerate(revs[:-1])])
cur.execute("INSERT INTO ps2 (page_id,rev_id,undone,row_start) VALUES (%s,%s,%s,%s)", (42, revs[-1]["rev_id"], und[-1], fmt(ts_b[-1]))); con.commit()
def snap(t):
    cur.execute("SELECT MAX(row_start) FROM ps2 FOR SYSTEM_TIME ALL WHERE page_id=%s AND row_start <= %s AND undone = 0", (42, t))
    return cur.fetchone()[0]
random.seed(5); bad = inside = fixed = blank_without_snap = 0
blank_hash = hashes_per[[i for i, r in enumerate(revs) if r["comment"] == "vandalism"][0]]
for _ in range(1000):
    x = ts_b[0] + (ts_b[-1] - ts_b[0]) * random.random(); i = bisect.bisect_right(ts_b, x) - 1; j = i
    while und[j]: j -= 1
    s = snap(fmt(x)); bad += (s != ts_b[j])
    if und[i]:
        inside += 1; blank_without_snap += reconstruct(fmt(x)) == hashes_per[i]; fixed += reconstruct(fmt(s)) == hashes_per[j]
print(f"snap_ts equals last surviving revision: {1000-bad}/1000")
print(f"instants inside an undone (vandalised) window: {inside}; plain AS OF returned the vandalised state in {blank_without_snap}; snapped AS OF returned the pre-vandalism state in {fixed}")
```

---

## Appendix C. SQL checks to re-run on the real server

`pitfalls.sql` (expected results on 10.11 are in its header; compare them on 12.3.x):

```sql
-- pitfalls.sql: behaviours the plan depends on. Run as an ordinary user in a scratch database,
--   mariadb -u USER -pPASS DB --force --table < pitfalls.sql
-- Observed on MariaDB 10.11.14 (the sandbox). Re-run on the real server (12.3.x) and compare:
--   T1: old / new / old / 0                       AS OF boundaries: row_start inclusive, row_end exclusive
--   LIFE: 7,8 / 8 / 7,8 / 7:222,8:333 / 7 / 0      deletion and revival with the same primary key (gap in validity)
--   ZERO-LEN history row: ERROR 1525 (Incorrect row_start value)  -> the 1 microsecond bump is mandatory
--   ALIAS after the FOR SYSTEM_TIME clause: works; alias before it: syntax error 1064
--   CTE + AS OF + IN (subquery) + join: works
--   LIMIT directly inside IN (subquery): ERROR 1235; wrapped in a derived table: works
--   FLIPS: claim 20 -> 3, claim 21 -> 1;  CHAIN broken: 0
--   PREPARE with a bare date string: accepted (n = 2) on 10.11 (the starter reports a refusal with its client)
--   UPDATE on a versioned row: adds a history row stamped with the real clock
--   CREATE TEMPORARY TABLE ... WITH SYSTEM VERSIONING: ERROR 4137
--   ALTER ADD COLUMN / MODIFY COLUMN: ERROR 4119 unless system_versioning_alter_history=KEEP; ADD KEY works
SET time_zone='+00:00';
SET system_versioning_insert_history=1;

-- T1: backdated history rows as a normal user
DROP TABLE IF EXISTS t1;
CREATE TABLE t1 (id INT PRIMARY KEY, v VARCHAR(9),
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)) WITH SYSTEM VERSIONING;
INSERT INTO t1 (id,v,row_start,row_end) VALUES (1,'old','2010-01-01 00:00:00','2012-06-01 00:00:00');
INSERT INTO t1 (id,v,row_start)         VALUES (1,'new','2012-06-01 00:00:00');
SELECT 'T1 mid (expect old)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2011-01-01 00:00:00';
SELECT 'T1 at boundary (expect new)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2012-06-01 00:00:00';
SELECT 'T1 boundary-1us (expect old)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2012-05-31 23:59:59.999999';
SELECT 'T1 before first (expect 0)' t, COUNT(*) n FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2009-01-01 00:00:00';

-- claim table: lifecycle with deletion and revival (same PK, gap in validity)
DROP TABLE IF EXISTS c;
CREATE TABLE c (page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL, text_hash BIGINT UNSIGNED NOT NULL, rev_id INT UNSIGNED NOT NULL,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id, claim_id), KEY (page_id, row_start), KEY (text_hash)) WITH SYSTEM VERSIONING;
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,7,111,1,'2011-01-01 00:00:00','2012-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,7,111,3,'2013-01-01 00:00:00','2014-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start)         VALUES (1,7,222,4,'2014-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,8,333,1,'2011-01-01 00:00:00','2015-01-01 00:00:00');
SELECT 'LIFE 2011-06 (expect 7,8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00';
SELECT 'LIFE 2012-06 gap (expect 8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2012-06-01 00:00:00';
SELECT 'LIFE 2013-06 revived (expect 7,8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2013-06-01 00:00:00';
SELECT 'LIFE 2014-06 (expect 7,8; 7 has 222)' t, GROUP_CONCAT(CONCAT(claim_id,':',text_hash) ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2014-06-01 00:00:00';
SELECT 'LIFE 2016 (expect 7)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2016-01-01 00:00:00';
SELECT 'LIFE current rows per PK > 1 (expect 0)' t, COUNT(*) n FROM (SELECT page_id, claim_id FROM c GROUP BY page_id, claim_id HAVING COUNT(*) > 1) x;

-- zero-length history row (what same-second edits would produce without the 1 us bump)
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,9,444,1,'2012-01-01 00:00:00','2012-01-01 00:00:00');
SELECT 'ZERO-LEN accepted? rows for claim 9' t, COUNT(*) n FROM c FOR SYSTEM_TIME ALL WHERE claim_id=9;

-- alias placement with FOR SYSTEM_TIME
SELECT 'ALIAS after clause' t, k.claim_id FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00' AS k WHERE k.claim_id=8;
SELECT 'ALIAS before clause' t, k.claim_id FROM c AS k FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00' WHERE k.claim_id=8;

-- CTE + AS OF + IN (subquery) + join (shape of the retrieval query)
CREATE TABLE ch (text_hash BIGINT UNSIGNED PRIMARY KEY, txt TEXT, score INT);
INSERT INTO ch VALUES (111,'a',1),(222,'b',2),(333,'c',3),(444,'d',4);
WITH routed AS (SELECT 1 AS page_id),
snap AS (SELECT page_id, claim_id, text_hash FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2014-06-01 00:00:00' WHERE page_id IN (SELECT page_id FROM routed))
SELECT 'CTE ok' t, s.claim_id, k.txt FROM snap s JOIN ch k ON k.text_hash = s.text_hash ORDER BY k.score DESC LIMIT 8;

-- LIMIT inside IN (subquery): direct vs wrapped in a derived table
SELECT 'LIMIT direct' t, claim_id FROM c FOR SYSTEM_TIME ALL WHERE text_hash IN (SELECT text_hash FROM ch ORDER BY score LIMIT 2);
SELECT 'LIMIT wrapped (R2 shape)' t, GROUP_CONCAT(DISTINCT page_id) pages FROM c FOR SYSTEM_TIME ALL WHERE text_hash IN (SELECT text_hash FROM (SELECT text_hash FROM ch ORDER BY score LIMIT 2) AS n);

-- flips via LAG on FOR SYSTEM_TIME ALL: A,B,A,B,A on claim 20 and A,B,A on claim 21
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES
 (2,20,1,1,'2011-01-01','2011-02-01'),(2,20,2,2,'2011-02-01','2011-03-01'),(2,20,1,3,'2011-03-01','2011-04-01'),(2,20,2,4,'2011-04-01','2011-05-01'),
 (2,21,1,1,'2011-01-01','2011-02-01'),(2,21,2,2,'2011-02-01','2011-03-01');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start) VALUES (2,20,1,5,'2011-05-01'),(2,21,1,3,'2011-03-01');
SELECT 'FLIPS (expect 20->3, 21->1)' t, claim_id, SUM(text_hash = h2 AND text_hash <> h1) AS flips
FROM (SELECT page_id, claim_id, text_hash, LAG(text_hash,1) OVER w AS h1, LAG(text_hash,2) OVER w AS h2
      FROM c FOR SYSTEM_TIME ALL WHERE page_id=2
      WINDOW w AS (PARTITION BY page_id, claim_id ORDER BY row_start)) x GROUP BY claim_id;

-- chain integrity query (F2) on claim 20/21 histories
SELECT 'CHAIN broken (expect 0)' t, COUNT(*) broken FROM (
  SELECT page_id, claim_id, row_start, row_end, LEAD(row_start) OVER (PARTITION BY page_id, claim_id ORDER BY row_start) AS next_start
  FROM c FOR SYSTEM_TIME ALL WHERE page_id=2) h
WHERE row_end <= row_start OR (next_start IS NOT NULL AND row_end <> next_start);

-- server-side prepared statement: bare date vs full datetime
PREPARE s FROM 'SELECT COUNT(*) n FROM c FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id=1';
SET @t='2011-06-01'; EXECUTE s USING @t;
SET @t='2011-06-01 00:00:00'; EXECUTE s USING @t;

-- UPDATE on a versioned row writes a history row stamped with the real clock
SELECT 'UPD before: history rows claim 8' t, COUNT(*) n FROM c FOR SYSTEM_TIME ALL WHERE claim_id=7 AND page_id=1;
UPDATE c SET rev_id = 99 WHERE page_id=1 AND claim_id=7;
SELECT 'UPD after: history rows claim 7' t, COUNT(*) n, MAX(row_start) latest_start FROM c FOR SYSTEM_TIME ALL WHERE claim_id=7 AND page_id=1;

-- temporary + versioned, ALTER variants
CREATE TEMPORARY TABLE tt (id INT) WITH SYSTEM VERSIONING;
ALTER TABLE c ADD COLUMN extra INT NULL;
ALTER TABLE c MODIFY COLUMN extra BIGINT NULL;
ALTER TABLE c ADD KEY (rev_id);
SELECT 'ALTER done' t, @@system_versioning_alter_history ah;
```

`routing_shape.sql` (the headline query's structure on dummy numeric distances; replace the stand-ins with `VEC_DISTANCE_COSINE` and `VEC_FromText` once the vector tables exist):

```sql
SET time_zone='+00:00'; SET system_versioning_insert_history=1;
DROP TABLE IF EXISTS wikirag.article_vector; DROP TABLE IF EXISTS wikihistory.chunk; DROP TABLE IF EXISTS wikihistory.claim;
CREATE TABLE wikirag.article_vector (lang CHAR(2), page_id INT UNSIGNED, embedding DOUBLE NOT NULL, PRIMARY KEY (lang, page_id));
CREATE TABLE wikihistory.chunk (text_hash BIGINT UNSIGNED PRIMARY KEY, text TEXT NOT NULL, emb DOUBLE NOT NULL, FULLTEXT (text)) ENGINE=InnoDB;
CREATE TABLE wikihistory.claim (page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL, text_hash BIGINT UNSIGNED NOT NULL, section VARCHAR(255), rev_id INT UNSIGNED NOT NULL, editor VARCHAR(255),
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE, row_end TIMESTAMP(6) GENERATED ALWAYS AS ROW END INVISIBLE, PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id, claim_id), KEY (page_id, row_start), KEY (text_hash)) WITH SYSTEM VERSIONING;
INSERT INTO wikirag.article_vector VALUES ('fi',1,4.0),('fi',2,9.0),('fi',3,5.2),('sv',9,5.0);
INSERT INTO wikihistory.chunk VALUES (11,'Mannerheim johti puolustusvoimia',5.1),(12,'Kekkonen oli presidentti',8.0),(13,'Talvisota alkoi marraskuussa',4.9),(14,'Mannerheimin elämä ja ura',20.0);
INSERT INTO wikihistory.claim (page_id,claim_id,text_hash,section,rev_id,editor,row_start,row_end) VALUES
 (1,1,11,'a',1,'x','2010-01-01','2013-01-01'),(1,1,13,'a',2,'y','2013-01-01','2016-01-01'),
 (2,1,12,'b',3,'x','2010-01-01','2020-01-01'),(5,1,14,'c',4,'z','2011-01-01','2012-01-01');
INSERT INTO wikihistory.claim (page_id,claim_id,text_hash,section,rev_id,editor,row_start) VALUES (1,1,13,'a',5,'y','2016-01-01'),(5,1,14,'c',6,'z','2012-01-01');
SET @q = 5;
WITH r1 AS (
  SELECT page_id FROM (
    SELECT page_id, ABS(embedding - @q) AS d FROM wikirag.article_vector WHERE lang = 'fi' ORDER BY d LIMIT 2) AS a),
r2 AS (
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk ORDER BY ABS(emb - @q) LIMIT 2) AS n)),
r3 AS (
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk WHERE MATCH(text) AGAINST ('mannerheim*' IN BOOLEAN MODE) LIMIT 200) AS m)),
routed AS (SELECT page_id FROM (SELECT page_id FROM r1 UNION SELECT page_id FROM r2 UNION SELECT page_id FROM r3) u LIMIT 25),
snap AS (
  SELECT page_id, claim_id, text_hash, section, rev_id, editor
  FROM wikihistory.claim FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00'
  WHERE page_id IN (SELECT page_id FROM routed))
SELECT s.page_id, s.claim_id, s.rev_id, s.editor, c.text, ABS(c.emb - @q) AS d
FROM snap s JOIN wikihistory.chunk c ON c.text_hash = s.text_hash
ORDER BY d LIMIT 8;
SELECT 'routed pages (expect 1,2,3 from r1; 1 from r2/r3; 5 via r3 prefix)' note;
WITH r3 AS (SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL WHERE text_hash IN (SELECT text_hash FROM (SELECT text_hash FROM wikihistory.chunk WHERE MATCH(text) AGAINST ('mannerheim*' IN BOOLEAN MODE) LIMIT 200) AS m)) SELECT GROUP_CONCAT(page_id ORDER BY page_id) r3_pages FROM r3;
SELECT 'prefix vs inflected form' note, (SELECT COUNT(*) FROM wikihistory.chunk WHERE MATCH(text) AGAINST ('mannerheim*' IN BOOLEAN MODE)) prefix_hits, (SELECT COUNT(*) FROM wikihistory.chunk WHERE MATCH(text) AGAINST ('mannerheim' IN BOOLEAN MODE)) bare_hits, (SELECT COUNT(*) FROM wikihistory.chunk WHERE MATCH(text) AGAINST ('mannerheimin' IN BOOLEAN MODE)) genitive_hits;
```

---

## Sources

- [S1] MariaDB/mariadb-wikipedia-datasets, README: datasets S, M and L and their releases. https://github.com/MariaDB/mariadb-wikipedia-datasets
- [S2] MariaDB/mariadb-system-versioning-wikipedia, README: the Wikipedia Time Machine, its measurements and its open items. https://github.com/MariaDB/mariadb-system-versioning-wikipedia
- [S3] MariaDB/mariadb-vector-wikipedia-rag, README: cross-language search, evaluation, index tuning and open questions. https://github.com/MariaDB/mariadb-vector-wikipedia-rag
- [S4] MariaDB Vector overview slides by Sergei Golubchik: one vector index per table, NOT NULL vectors, no partitioning, binary vector parameters. https://mariadb.org/wp-content/uploads/2025/02/vector-search.html
- [S5] MariaDB Jira MDEV-35418, better support for multiple duplicate vectors in the index. https://jira.mariadb.org/browse/MDEV-35418
- [S6] MariaDB Jira MDEV-35246, vector search skips a row in the table. https://jira.mariadb.org/browse/MDEV-35246
- [S7] WikiWho on mediawiki.org: supported wikis and the tools powered by it. https://www.mediawiki.org/wiki/WikiWho
- [S8] WikiWho API (GESIS) README: token-level provenance. https://github.com/gesiscss/wikiwho_api_public
- [S9] Junction 2026 dates, venue and team size, as announced in a LinkedIn post by Joel Lahtinen. https://fi.linkedin.com/in/joellahtinen
- [S10] The challenge brief supplied with the task (Junction 2026, MariaDB challenge text).
