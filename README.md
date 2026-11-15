# Hindsight

Ask Wikipedia what it knew, when it knew it. Junction 2026, MariaDB challenge (RAG + system-versioned tables).
Plan: `docs/plan.md`. Validation status: `docs/dashboard.html`, rendered from `docs/validation/*.json`.
Decisions and measured surprises: `docs/findings.md`.

## Status

| Step | Feature | State |
|---|---|---|
| 0 | F1 environment and smoke tests | validated |
| 1 | F2 corpus layer `page_state` and hero set | validated |
| 2 | F3 chunker and hashing | validated |
| 3 | F4 lineage resolver | validated (golden-set labels are from two model judges, not humans) |
| 4 | F5 embeddings and `chunk` store; F4S semantic lineage | validated (F5.6 and the semantic-link gate F4.3s fail as specified: the semantic pass is not adopted) |
| 5 | F6 deep layer: `claim`, `layout`, `text_variant` history | programmatic checks pass; awaiting user review |

Not in git (rebuilt by the commands below): `data/` (datasets and derived Parquet, ~560 MB), `.mariadb/` (the local
database), `starters/` (the challenge's starter repositories, cloned), `.venv/`.

The database user and password in `db/bootstrap.sql`, `db/server.sh` and `eval/common.py` (`hindsight`/`hindsight`) are
local development credentials for a server bound to 127.0.0.1 only (`eval/common.py` reads `DB_USER` / `DB_PASSWORD` if
set). Never reuse them on a reachable server.

## Step 0: environment (macOS, no Docker)

```sh
brew install mariadb@12.3
ollama serve &  ollama pull bge-m3
db/server.sh start            # MariaDB 12.3 on 127.0.0.1:3306 (datadir .mariadb/), schemas wikirag/wikihistory/smoke, user hindsight

uv venv --python 3.12 .venv
PATH="$(brew --prefix mariadb@12.3)/bin:$PATH" uv pip install --python .venv/bin/python -r requirements-dev.txt

# datasets: M (315 MB) and the S checksums plus S article files, from the official releases
B=https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/download
mkdir -p data/m data/s
(cd data/m && curl -sSfLO $B/m-2026-10/SHA256SUMS && for f in $(awk '{print $2}' SHA256SUMS); do curl -sSfLO $B/m-2026-10/$f; done)
(cd data/s && for f in SHA256SUMS README.md articles-fi.parquet articles-sv.parquet articles-en.parquet; do curl -sSfLO $B/s-2026-10/$f; done)

# load S with the RAG starter's own setup, pointed at our server
git clone --depth 1 https://github.com/MariaDB/mariadb-vector-wikipedia-rag starters/mariadb-vector-wikipedia-rag
printf 'DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_USER=hindsight\nDB_PASSWORD=hindsight\nDB_NAME=wikirag\n' > starters/mariadb-vector-wikipedia-rag/.env
mkdir -p logs
(cd starters/mariadb-vector-wikipedia-rag && time PATH="$(brew --prefix mariadb@12.3)/bin:$PATH" ./setup.sh --local) > logs/s_load.log 2>&1

# F1: every check, then the dashboard (about 5 minutes)
(cd eval && ../.venv/bin/python f1_check.py; ../.venv/bin/python render.py)
```

## Step 1: corpus layer `page_state` (all of dataset M) and the hero set

```sh
.venv/bin/python pipeline/load_page_state.py --fresh   # 862,076 versions with real edit times, ~5 s; resumable without --fresh
.venv/bin/python pipeline/triage.py                    # ranks pages, freezes docs/hero.tsv + docs/hero.md
(cd pipeline && ../.venv/bin/python -m pytest -q)      # revert-flag and bump golden cases
(cd eval && ../.venv/bin/python f2_check.py; ../.venv/bin/python render.py)   # F2, ~15 s (calls the fi.wikipedia API once)
```

## Step 2: chunk and hash every hero-page revision

```sh
.venv/bin/python pipeline/chunk_hero.py                # 39,418 revisions -> data/chunks/*.parquet, ~22 s; prints N
(cd pipeline && ../.venv/bin/python -m pytest -q)      # chunker + revision golden cases
(cd eval && ../.venv/bin/python f3_check.py; ../.venv/bin/python render.py)   # F3, ~3 min (compares with the starter on every distinct text)
```

## Step 3: claim lineage

```sh
.venv/bin/python pipeline/lineage.py                   # claim ids, events (with the deciding rule) and claim states -> data/lineage/, ~14 s
.venv/bin/python eval/f4_pool.py                       # only to redraw the golden set: then two judges label f4_pool_blind.json and
                                                       # f4_labels.json = {"blind_sha256": <its sha256>, "judges": [...]}
(cd eval && ../.venv/bin/python f4_mutate.py)          # fault injection, ~1.5 min (re-run after changing the resolver or f4_check)
(cd eval && ../.venv/bin/python f4_check.py; ../.venv/bin/python render.py)   # F4, ~1 min (replays all 60 pages)
```

## Step 4: embeddings, `chunk` store, semantic lineage (measured, not adopted)

```sh
.venv/bin/python pipeline/embed_plan.py                # model vs copy per distinct chunk (the plan's copy shortcut is off: F5.5)
.venv/bin/python pipeline/embed_chunks.py              # bge-m3 via Ollama, resumable -> data/embed/store/ (~16 min)
.venv/bin/python pipeline/load_chunks.py               # `chunk` with HNSW + FULLTEXT, resumable, verifies existing rows
.venv/bin/python eval/f5_kill.py                       # F5.8 evidence: SIGKILL the real embedding mid-run, restart (empty store only)
.venv/bin/python eval/f5_kill.py --load                # ... and the real load (empty table only)
(cd pipeline && ../.venv/bin/python -m pytest -q)      # includes the embedding job's resume tests
(cd eval && ../.venv/bin/python f5_check.py)           # F5, ~6 min
.venv/bin/python pipeline/lineage.py --semantic        # lineage with the semantic pass -> data/lineage_sem/ (data/lineage/ untouched)
.venv/bin/python eval/f4_pool.py --run sem             # its golden-set pool (then two judges: f4s_labels.json)
(cd eval && ../.venv/bin/python f4_mutate.py --run sem; ../.venv/bin/python f4_check.py --run sem; ../.venv/bin/python render.py)
```

The semantic pass did not reach its precision bar (F4S), so Step 5 loads the character-only lineage in `data/lineage/`.

## Step 5: the deep layer (`claim`, `layout`, `text_variant`)

```sh
.venv/bin/python pipeline/load_claims.py --fresh       # claim and layout history rows on page_state's bumped times, ~2 s; resumable without --fresh
(cd eval && ../.venv/bin/python f6_mutate.py)          # fault injection on a copy in the smoke schema, ~1 min
(cd eval && ../.venv/bin/python f6_check.py; ../.venv/bin/python render.py)   # F6, ~1 min (every revision rebuilt through AS OF)
```

## Streamlit app: snapshots and local RAG

The UI has two views. **Historical snapshots** reconstructs a selected hero article from `page_state`, `layout`, `claim`,
and `text_variant`. **Local RAG questions** retrieves Wikipedia articles with MariaDB Vector and asks a local Ollama
model to answer from cited excerpts. It uses the starter's Finnish, Swedish, and English article corpus and precomputed
`bge-m3` vectors. This RAG corpus is the latest article snapshot and does not yet answer for a historical date. Answer
generation runs on the same machine as Streamlit; it does not call a hosted AI service.

After the database and Steps 1–5 data are loaded:

```sh
db/server.sh start
.venv/bin/streamlit run app.py
```

Streamlit Cloud installs only the lightweight app dependencies from `requirements.txt`; local pipeline and evaluation work
uses `requirements-dev.txt`. The app reads `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` from the environment.
Streamlit deployments can set the same values under `[database]` in `.streamlit/secrets.toml` (`host`, `port`, `user`,
`password`, `name`). The default settings target the project-local MariaDB bound to `127.0.0.1`; a hosted app needs a
network-reachable MariaDB host configured in its secrets to browse the full history. Without one, the app shows four
packaged Talvisota snapshots from `docs/demo_snapshots.json`, so the UI can be previewed without a hosted database. The
app and pipeline helpers use TLS with certificate verification for non-local database hosts; keep that enabled for
hosted databases. For local socket-based MariaDB, set `DB_SOCKET` to its socket path and use `DB_USER` / `DB_PASSWORD`
for a local account.

### Local RAG setup

The local RAG view needs MariaDB 11.7 or newer with the vector tables loaded, Ollama running, and the local models
`bge-m3` (embeddings) and `qwen3:8b` (answer generation). These models are configured by default; override them with
`RAG_EMBED_MODEL` and `RAG_CHAT_MODEL` if you use different local models. On the current macOS setup, MariaDB is
reachable through `/tmp/mysql.sock` and the prepared article and vector Parquet files are already in
`starters/mariadb-vector-wikipedia-rag/data/`.

Install the app and starter dependencies if needed, create the RAG schema, and load the prepared data. The commands
below use the current local MariaDB account; change `DB_USER` or `DB_SOCKET` for another installation. Loading the
article rows and vectors takes a few minutes the first time and is safe to repeat.

```sh
uv pip install --python .venv/bin/python -r requirements.txt
export DB_SOCKET=/tmp/mysql.sock
export DB_USER="$USER"
export DB_PASSWORD=

mariadb --no-defaults --protocol=SOCKET --socket="$DB_SOCKET" -u "$DB_USER" \
  -e "CREATE DATABASE IF NOT EXISTS wikirag CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"

(cd starters/mariadb-vector-wikipedia-rag && \
  DB_NAME=wikirag ../../.venv/bin/python create_tables.py && \
  DB_NAME=wikirag ../../.venv/bin/python load.py --vectors)
```

Start MariaDB and Ollama before launching Streamlit. If Ollama is not already running from its macOS app, use a
separate terminal for `ollama serve`. Install the model files once with `ollama pull bge-m3` and `ollama pull qwen3:8b`.
Then launch the app with the same local DB environment:

```sh
export DB_NAME=wikihistory
.venv/bin/streamlit run app.py
```

Streamlit Cloud can show the packaged historical snapshot sample, but it cannot reach the MariaDB and Ollama processes
on this Mac. The local RAG view is therefore for the locally running app unless you separately host both services.

Wikipedia text and everything derived from it is CC BY-SA 4.0.
