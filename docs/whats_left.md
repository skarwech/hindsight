# What's left

Status on 2026-11-15. Validation status per feature: `docs/dashboard.html`; decisions and measurements: `docs/findings.md`.

## Where things stand

| Step | Feature | State |
|---|---|---|
| 0–3 | F1–F4: environment, `page_state`, chunker, lineage resolver | validated |
| 4 | F5 embeddings and `chunk` store; F4S semantic lineage | validated: F5.6 (the plan's probe check) and the semantic-link gate fail as specified, so the semantic pass is not adopted |
| 5 | F6 deep layer: `claim`, `layout`, `text_variant` | all programmatic F6 checks pass; **awaiting user review** |
| 6–8 | F7–F13: time-aware retrieval, then/now, insight, audit | not started; a current-article local RAG baseline is now available |
| 9 | F14 web UI | Streamlit snapshot viewer and local RAG Q&A added; full UI and F14 validation remain |
| 10–12 | F15–F17: benchmark, stretch features, ship | not started |

## Left in Step 5 (before asking for validation)

The review fixes are implemented and the full validation ran on 2026-11-15. F6.1–F6.4, F6.6 and F6.7 pass; F6.5 records storage as information. The 8 fault cases all produced their expected failing sub-checks. Fresh results are in `docs/validation/f6.json`, `docs/validation/evidence/f6_mutations.json` and `docs/dashboard.html`.

Remaining Step 5 work: review the F6 results and accept the step. The Streamlit app now has historical snapshots and a local, current-article RAG Q&A view. Retrieval against claims as of a selected date, then-versus-now comparison, and the rest of F14 are still unimplemented.

## Open decisions (yours)

- **F5.6, the golden probes.** The plan's check (top 5 over every historical chunk) scores 5 of 10; ranking what is alive at the date scores 8 of 10 on the same probes and 38 of 40 on fresh ones. Proposed: restate F5.6 under that ranking and test it in F7, with probes about past dates.
- **A semantic pass without shrinks.** Every bad semantic link joined a paragraph to a fragment under half its length. Without those links, precision is 87.3% worst case and 95.4% on agreed labels, which is still short of 95% worst case, and the guard was chosen after seeing the labels. Trying it needs a fresh pool and new labels.
- **The mypy cleanup branch** `claude/dazzling-ishizaka-8c0233` (commit `efd700c`, not merged). It changes `pipeline/resolver.py`'s hash. After merging, re-run, in this order:
  - `pipeline/lineage.py`, then `pipeline/lineage.py --semantic`;
  - `eval/f4_mutate.py` and `eval/f4_mutate.py --run sem`;
  - `eval/f4_check.py` and `eval/f4_check.py --run sem`;
  - `pipeline/load_claims.py --fresh`, then `eval/f6_mutate.py` and `eval/f6_check.py`.
- **A human spot-check** of the golden sets (F4, F4S). Their labels come from two model judges, not people.

## Notes for Step 6 (retrieval, `/api/ask`, then versus now)

From the Step 4 and Step 5 reviews (details in `docs/findings.md`):

- Rank with exact cosine over the paragraphs alive at the chosen date in the routed pages. Use HNSW only to route, at `mhnsw_ef_search` ≥ 400 (`db/vector_search.json`). At ef 80, HNSW loses the whole answer on about 2% of questions.
- The plan's F7.1 "0 time-filter violations" conflicts with its own snapping rule, which queries `AS OF snap_ts` when t falls on an undone revision. Restate the gate for snapped dates.
- The mariadb connector drops time zones from aware datetimes, so bind naive UTC. `db/headline.sql` pastes the date into the SQL text: bind it instead.
- Quote the exact spelling live at t (`text_variant` via `claim.exact_hash`), not `chunk.text`.
- Take "the revision live at t" from `page_state`, not from `layout.rev_id` or `claim.rev_id`.
- A claim row opens on any state change, and its editor is whoever opened that state. Attribute text through the lineage events.
- Hidden revisions carry claims over. `page_state.title` is the current title for every version.

## Remaining plan steps (docs/plan.md, section 6)

| Step | What | Features |
|---|---|---|
| 6 | Retrieval (route, `AS OF`, exact ranking, hybrid), `/api/ask`, then versus now | F7, F8, F9 |
| 7 | Contest score, claim X-ray and biography, dispute summaries | F10, F11, F12 |
| 8 | Audit and vintage | F13 |
| 9 | Web UI | F14 |
| 10 | Evaluation harness and benchmark sheet | F15 |
| 11 | Stretch: overlay and gap map | F16 |
| 12 | Freeze and ship (demo hardening) | F17 |

## Resuming on this machine

```sh
cd ~/hindsight
db/server.sh start                                   # MariaDB 12.3, datadir .mariadb/ (not in git)
ollama serve &                                       # bge-m3
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt   # if .venv is missing
.venv/bin/python -m http.server 8766 --bind 127.0.0.1 --directory docs   # dashboard at http://localhost:8766/dashboard.html
```

- `data/`, `.mariadb/` and `starters/` are not in git; the README rebuilds them, step by step.
- Re-running F1 in full needs the RAG starter's own virtual environment (`starters/mariadb-vector-wikipedia-rag/.venv`) and its `.env`. Neither came along when the project moved folders, so recreate them as in Step 0 of the README.
- After the Step 5 schema change, F1's T4 was re-run on its own and passes.
