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
| 4 | F5 embeddings and `chunk` store, semantic lineage | in progress, not in this repository yet |

Not in git (rebuilt by the commands below): `data/` (datasets and derived Parquet, ~560 MB), `.mariadb/` (the local
database), `starters/` (the challenge's starter repositories, cloned), `.venv/`. `pipeline/lineage.py --semantic` and
`eval/f4_check.py --run sem` belong to Step 4 and need its files.

The database user and password in `db/bootstrap.sql`, `db/server.sh` and `eval/common.py` (`hindsight`/`hindsight`) are
local development credentials for a server bound to 127.0.0.1 only (`eval/common.py` reads `DB_USER` / `DB_PASSWORD` if
set). Never reuse them on a reachable server.

## Step 0: environment (macOS, no Docker)

```sh
brew install mariadb@12.3
ollama serve &  ollama pull bge-m3
db/server.sh start            # MariaDB 12.3 on 127.0.0.1:3306 (datadir .mariadb/), schemas wikirag/wikihistory/smoke, user hindsight

uv venv --python 3.12 .venv
PATH="$(brew --prefix mariadb@12.3)/bin:$PATH" uv pip install --python .venv/bin/python -r requirements.txt

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

Wikipedia text and everything derived from it is CC BY-SA 4.0.
