-- schema wikihistory (dataset S lives in schema wikirag, loaded by the RAG repo's own scripts)

CREATE TABLE page_state (                      -- corpus layer: metadata of every version, real edit times
  page_id   INT UNSIGNED NOT NULL,
  title     VARCHAR(255) NOT NULL,             -- titles change over time (page moves)
  rev_id    INT UNSIGNED NOT NULL,
  editor    VARCHAR(255) COLLATE utf8mb4_bin, anon BOOL NOT NULL, minor BOOL NOT NULL,   -- bin: user names compare exactly (ai_ci would merge Käyttäjä and Kayttaja)
  comment   TEXT, sha1 CHAR(31) CHARACTER SET ascii COLLATE ascii_bin, bytes INT UNSIGNED,
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
  exact_hash BIGINT UNSIGNED NOT NULL,         -- the exact spelling (text_variant), so a revision is quoted as it was
  section   VARCHAR(255),
  rev_id    INT UNSIGNED NOT NULL,             -- revision that wrote this state
  editor    VARCHAR(255) COLLATE utf8mb4_bin, anon BOOL NOT NULL,
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

CREATE TABLE text_variant (                    -- every exact spelling of a chunk (Step 2: punctuation, case, spacing differ)
  exact_hash BIGINT UNSIGNED PRIMARY KEY,      -- xxh64 of the exact text
  text_hash  BIGINT UNSIGNED NOT NULL,         -- its normalised hash: the chunk row (and vector) it belongs to
  text       TEXT NOT NULL,
  KEY (text_hash)
);

CREATE TABLE claim_stats (                     -- plain table: signals and summaries per claim
  page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL,
  n_states SMALLINT UNSIGNED, n_edits SMALLINT UNSIGNED, n_named SMALLINT UNSIGNED, n_anon SMALLINT UNSIGNED,
  flips SMALLINT UNSIGNED, named_flips SMALLINT UNSIGNED, alternation SMALLINT UNSIGNED,
  revivals SMALLINT UNSIGNED, named_reverts SMALLINT UNSIGNED, uncited_years DECIMAL(4,1),
  first_ts TIMESTAMP(6) NULL, last_ts TIMESTAMP(6) NULL,
  contest FLOAT, kind VARCHAR(16), summary TEXT,
  PRIMARY KEY (page_id, claim_id)
);
