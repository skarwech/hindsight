"""F5.8 evidence: run the real embedding job, SIGKILL it in the middle of a shard, restart it, let it finish.

    .venv/bin/python eval/f5_kill.py [--at-shard 20]     # on an empty store; ~15 min; writes evidence/f5_kill.json
    .venv/bin/python eval/f5_kill.py --load [--at-rows 12000]   # the same for pipeline/load_chunks.py: evidence/f5_load_kill.json

The kill lands a random fraction of a shard after shard N is renamed into place, so the model is mid-batch (a torn
shard write and the .tmp cleanup are covered by pipeline/test_embed_chunks.py). The evidence records the plan's sha256,
the finished store's content digest and the job's code hash; F5.8 requires them to match the current plan, store and
code, and F5.1 compares the store with the plan: every planned hash exactly once, no duplicates, nothing extra.
"""
import argparse, hashlib, json, random, signal, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
STORE, OUT = ROOT / "data/embed/store", ROOT / "docs/validation/evidence/f5_kill.json"
JOB = [sys.executable, str(ROOT / "pipeline/embed_chunks.py")]
LOAD = [sys.executable, str(ROOT / "pipeline/load_chunks.py")]
sys.path.insert(0, str(ROOT / "pipeline"))


def provenance(script: str) -> dict:
    """What the evidence is bound to: the plan, the finished store's content and the job's code (F5.8 compares them)."""
    from embed_chunks import store_digest
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(plan_sha256=json.loads((ROOT / "data/embed/plan.json").read_text(encoding="utf-8"))["plan_sha256"],
                store_sha256=store_digest(), code_sha256=sha(ROOT / "pipeline" / script))


def kill_on_error(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill(); proc.wait()


def table_rows() -> int:
    from common import connect
    cur = connect("wikihistory").cursor()
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'wikihistory' AND table_name = 'chunk'")
    if not cur.fetchone()[0]:
        return 0
    cur.execute("SELECT COUNT(*) FROM chunk")
    return cur.fetchone()[0]


def kill_load(at_rows: int) -> None:
    if table_rows():
        sys.exit("chunk already has rows: the load kill test starts from an empty table")
    t0 = time.time()
    first = subprocess.Popen(LOAD, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        while table_rows() < at_rows:
            if first.poll() is not None:
                sys.exit(f"load ended before the kill point: {first.stderr.read()[-500:]}")
            time.sleep(0.1)
    finally:
        kill_on_error(first)                               # also the planned kill: SIGKILL, like the embedding test
    after_kill = dict(rows=table_rows(), returncode=first.returncode)
    second = subprocess.run(LOAD, capture_output=True, text=True)
    restart = json.loads(second.stdout.strip().splitlines()[-1]) if second.returncode == 0 else None
    out = OUT.with_name("f5_load_kill.json")
    out.write_text(json.dumps(dict(kill_at_rows=at_rows, **provenance("load_chunks.py"), after_kill=after_kill,
                                   restart_returncode=second.returncode, restart=restart,
                                   stderr=second.stderr[-2000:], seconds=round(time.time() - t0, 1)), indent=1), encoding="utf-8")
    print(out.read_text())


def shards() -> int:
    return len(list(STORE.glob("part-*.parquet")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at-shard", type=int, default=20)
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--at-rows", type=int, default=12000)
    a = ap.parse_args()
    if a.load:
        return kill_load(a.at_rows)
    if shards():
        sys.exit("store is not empty: the kill test starts from nothing (move data/embed/store away first)")
    t0 = time.time()
    first = subprocess.Popen(JOB, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        while shards() < a.at_shard:
            if first.poll() is not None:
                sys.exit(f"job ended before the kill point: {first.stderr.read()[-500:]}")
            time.sleep(0.2)
        delay = random.Random(5).uniform(2.0, 12.0)        # a shard of 512 texts takes ~16 s: the kill lands mid-batch
        time.sleep(delay)
        at_kill = dict(shards=shards(), tmp_files=[p.name for p in STORE.glob(".*.tmp")])
        first.send_signal(signal.SIGKILL); first.wait()
    finally:
        kill_on_error(first)
    after_kill = dict(shards=shards(), tmp_files=[p.name for p in STORE.glob(".*.tmp")], returncode=first.returncode)
    second = subprocess.run(JOB, capture_output=True, text=True)
    result = json.loads(second.stdout.strip().splitlines()[-1]) if second.returncode == 0 else None
    OUT.write_text(json.dumps(dict(kill_after_shard=a.at_shard, **provenance("embed_chunks.py"), delay_s=round(delay, 1),
                                   at_kill=at_kill, after_kill=after_kill,
                                   restart_returncode=second.returncode, restart=result, stderr=second.stderr[-2000:],
                                   seconds=round(time.time() - t0, 1)), indent=1), encoding="utf-8")
    print(OUT.read_text())


if __name__ == "__main__":
    main()
