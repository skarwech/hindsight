"""The embedding job's resume guarantees (F5.8), with a fake embedder and a scratch plan, store and chunk table.
    cd pipeline && ../.venv/bin/python -m pytest -q test_embed_chunks.py"""
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import embed_chunks as E


def fake_embed(texts: list[str]) -> np.ndarray:
    v = np.array([[hash(t) % 97 + 1, len(t), 1.0] + [0.0] * (E.DIM - 3) for t in texts], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emb, ch = tmp_path / "embed", tmp_path / "chunks"
    emb.mkdir(); ch.mkdir()
    monkeypatch.setattr(E, "EMB", emb); monkeypatch.setattr(E, "CH", ch); monkeypatch.setattr(E, "embed", fake_embed)
    monkeypatch.setattr(E, "SHARD", 2); monkeypatch.setattr(E, "CALL", 2)
    texts = {h: f"paragraph number {h} " * 3 for h in range(1, 8)}
    pq.write_table(pa.table({"text_hash": pa.array(list(texts), pa.uint64()), "text": list(texts.values())}), ch / "chunk_text.parquet")

    def plan(actions: dict[int, tuple[str, int | None]]) -> None:
        hs = sorted(actions)
        pq.write_table(pa.table({"text_hash": pa.array(hs, pa.uint64()), "action": [actions[h][0] for h in hs],
                                 "anchor": pa.array([actions[h][1] for h in hs], pa.uint64()),
                                 "first_page": pa.array([1] * len(hs), pa.uint32()), "first_ord": pa.array(hs, pa.uint32())}),
                       emb / "plan.parquet")
    return tmp_path / "store", plan


def sources(store: Path) -> dict[int, str]:
    h, src, _ = E.read_store(store)
    return dict(zip(h.tolist(), src, strict=True))


def test_resume_after_a_partial_run_finishes_with_each_hash_once(world) -> None:
    store, plan = world
    plan({h: ("model", None) for h in range(1, 8)})
    first = E.run(store, limit=3)
    assert first["rows_after"] == 3 and not first["complete"]
    done = E.run(store)
    h, _, _ = E.read_store(store)
    assert done["complete"] and sorted(h.tolist()) == list(range(1, 8)) and len(h) == len(set(h.tolist()))


def test_a_temp_file_left_by_a_kill_is_removed_and_ignored(world) -> None:
    store, plan = world
    plan({h: ("model", None) for h in range(1, 8)})
    store.mkdir(); (store / ".part-00000.parquet.tmp").write_bytes(b"torn write")
    assert E.run(store)["complete"] and not list(store.glob(".*.tmp"))


def test_copies_take_their_anchor_vector(world) -> None:
    store, plan = world
    plan({1: ("model", None), 2: ("copy", 1), 3: ("model", None)})
    E.run(store)
    h, src, vec = E.read_store(store)
    row = {int(x): k for k, x in enumerate(h)}
    assert sources(store) == {1: "model", 2: "copy", 3: "model"} and np.array_equal(vec[row[2]], vec[row[1]])


def test_a_store_that_disagrees_with_a_changed_plan_is_refused(world) -> None:   # review: stale copies were kept as complete
    store, plan = world
    plan({1: ("model", None), 2: ("copy", 1), 3: ("model", None)})
    E.run(store)
    plan({1: ("model", None), 2: ("model", None), 3: ("model", None)})
    with pytest.raises(RuntimeError, match="disagree with the plan"):
        E.run(store)


def test_a_gap_in_shard_numbers_never_overwrites_the_last_shard(world) -> None:   # review: names were part-{count}
    store, plan = world
    plan({h: ("model", None) for h in range(1, 8)})
    E.run(store, limit=4)                                  # part-00000, part-00001
    (store / "part-00000.parquet").rename(store / "moved.bak")
    before = (store / "part-00001.parquet").read_bytes()
    E.run(store, limit=2)
    assert (store / "part-00001.parquet").read_bytes() == before and (store / "part-00002.parquet").exists()
