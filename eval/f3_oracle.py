"""The F3 oracle: the starter's own history.clean(), with the five documented fixes applied around it, written
independently of pipeline/chunker.py where the fix lives inside the cleaner (the template dropper)."""
import html, importlib.util, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
from chunker import norm, preprocess  # noqa: E402  (fixes 1, 3, 5 are text-in/text-out and shared on purpose)

_spec = importlib.util.spec_from_file_location("starter_history", ROOT / "starters/mariadb-system-versioning-wikipedia/history.py")
starter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(starter)  # type: ignore[union-attr]


def drop_nested(text: str, start: str, end: str) -> str:
    """Character loop (not the chunker's regex scan): matched spans dropped, a stray end kept, an unclosed start
    dropped through the end of its line (fix 2). On balanced text this is the starter's _drop_nested."""
    stack, edges, i = [], [0] * (len(text) + 1), 0
    while i < len(text):
        if text.startswith(start, i):
            stack.append(i); i += len(start)
        elif stack and text.startswith(end, i):
            s = stack.pop(); i += len(end); edges[s] += 1; edges[i] -= 1
        else:
            i += 1
    for s in stack:
        e = text.find("\n", s); e = e if e >= 0 else len(text); edges[s] += 1; edges[e] -= 1
    out, depth = [], 0
    for i, ch in enumerate(text):
        depth += edges[i]
        if not depth:
            out.append(ch)
    return "".join(out)


starter._drop_nested = drop_nested                                   # fix 2, patched into the starter's module


def clean(wikitext: str) -> list[tuple[str, str]]:
    """The starter's clean() on the preprocessed text, entities decoded (fix 4)."""
    return [(k, html.unescape(t).strip()) for k, t in starter.clean(preprocess(wikitext or "", False))]




def line_refs(line: str) -> int:
    """Citations on one raw line that survive cleaning: refs inside templates, tables or comments do not count."""
    t = re.sub(r"<!--.*?-->", "", line, flags=re.S)
    t = re.sub(r"<references\b[^>]*/>|<references\b[^>]*>.*?</references\s*>", "", t, flags=re.I | re.S)
    t = re.sub(r"<ref(?!erences)[^>]*/>|<ref(?!erences)[^>]*>.*?</ref\s*>", "\x00", t, flags=re.I | re.S)
    t = drop_nested(drop_nested(t, "{{", "}}"), "{|", "|}")
    t = re.sub(r"\[\[(?:Tiedosto|Kuva|File|Image|Luokka|Category):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]", "", t)   # captions, nested links included
    return t.count("\x00")


def covered(blocks: list[tuple[str, str]], chunk_texts: list[str]) -> tuple[int, int]:
    """(normalised characters of the blocks found in the chunk text, total), over non-heading blocks."""
    joined = norm(" ".join(chunk_texts))
    total = hit = 0
    for k, t in blocks:
        if k == "h":
            continue
        n = norm(t); total += len(n)
        hit += len(n) if n and n in joined else 0
    return hit, total
