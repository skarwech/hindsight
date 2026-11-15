"""Shared helpers for the validation scripts: DB connection, embeddings, SQL-file runner, check log."""
import json, os, pathlib
from decimal import Decimal
from typing import Any

import mariadb
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"), port=int(os.environ.get("DB_PORT", 3306)),
          user=os.environ.get("DB_USER", "hindsight"), password=os.environ.get("DB_PASSWORD", "hindsight"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_OPTS = {"num_ctx": 8192, "num_batch": 8192}   # Ollama needs both, or long inputs are cut [S3]


def connect(database: str) -> mariadb.Connection:
    settings = DB.copy()
    if settings["host"].strip("[]").lower() not in {"localhost", "127.0.0.1", "::1"}:
        settings["ssl_verify_cert"] = True
    con = mariadb.connect(**settings, database=database, autocommit=True)
    con.cursor().execute("SET time_zone = '+00:00'")
    return con


def embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(f"{OLLAMA}/api/embed", timeout=600,
                      json={"model": "bge-m3", "input": texts, "truncate": True, "options": EMBED_OPTS})
    r.raise_for_status()
    return r.json()["embeddings"]


def split_sql(text: str) -> list[str]:
    """Split on ';' outside quotes, dropping -- comments. Enough for our own smoke files."""
    out, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch); quote = None if ch == quote else quote
        elif text.startswith("--", i):
            i = text.find("\n", i); i = len(text) if i < 0 else i; continue
        elif ch in "'\"":
            quote = ch; buf.append(ch)
        elif ch == ";":
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return [s for s in out if s]


def run_sql_file(con: mariadb.Connection, path: pathlib.Path) -> list[dict[str, Any]]:
    """Run every statement; keep going after errors (like mariadb --force)."""
    cur, results = con.cursor(), []
    for stmt in split_sql(pathlib.Path(path).read_text(encoding="utf-8")):
        try:
            cur.execute(stmt)
            rows = cur.fetchall() if cur.description else []
            cols = [d[0] for d in cur.description] if cur.description else []
            results.append(dict(sql=stmt, cols=cols, rows=[[_plain(v) for v in r] for r in rows], error=None))
        except mariadb.Error as e:
            results.append(dict(sql=stmt, cols=[], rows=[], error=[e.errno, str(e)]))
    return results


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):                       # SUM()/AVG() come back as Decimal
        return int(v) if v == v.to_integral_value() else float(v)
    return v if isinstance(v, (int, float, str, type(None))) else str(v)


class Checks:
    """Collects checks for one feature; status is pass / fail / info (recorded, not gated)."""

    def __init__(self, feature: str, title: str, gate: str) -> None:
        self.doc: dict[str, Any] = dict(feature=feature, title=title, gate=gate, checks=[], findings=[])

    def add(self, cid: str, name: str, kind: str, expect: Any, got: Any, ok: bool | None,
            evidence: Any = "") -> None:
        status = "info" if ok is None else ("pass" if ok else "fail")
        self.doc["checks"].append(dict(id=cid, name=name, kind=kind, expect=str(expect), got=str(got),
                                       status=status, evidence=str(evidence)[:20000]))
        print(f"[{status:4}] {cid} {name}: {got}", flush=True)

    def finding(self, text: str) -> None:
        self.doc["findings"].append(text)

    def table(self, title: str, cols: list[str], rows: list[list[Any]]) -> None:
        """A small result table shown under the checks (visual proof); values come from the run."""
        self.doc.setdefault("tables", []).append(dict(title=title, cols=cols, rows=[[_plain(v) for v in r] for r in rows]))

    def save(self, stamp: str) -> pathlib.Path:
        self.doc["generated"] = stamp
        out = ROOT / "docs" / "validation" / f"{self.doc['feature'].lower()}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")                 # atomic: a crash never leaves a half-written or stale-looking file
        tmp.write_text(json.dumps(self.doc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out)
        return out
