"""Step 2 (F3): one revision's wikitext -> ordered chunks (section, text, n_refs, text_hash).

Cleaning follows the starter's history.clean() (templates, tables, file and category links dropped; links become words;
headings and list items kept; lines starting with | ! ; dropped), with five deliberate fixes found by the F3 review,
all mirrored in the oracle of eval/f3_check.py:
  1. refs are matched case-insensitively and self-closing refs may contain "/" (<ref name="a/b"/> made the starter's
     paired regex swallow text up to the next </ref>: 1,631 hero revisions lost content);
  2. an unclosed {{ or {| drops only itself, not the rest of the article (13 real revisions had 0 chunks);
  3. lines indented with ":" are kept (the Declaration of Independence's own text is indented);
  4. HTML entities are decoded (8.5% of chunks carried &nbsp;, which hashed the word "nbsp");
  5. <references> lists are removed as such, not by accident.
Citations: every <ref> becomes a placeholder before cleaning and is counted per paragraph afterwards. A placeholder can
change how neighbouring markup parses in rare constructs (a ref before a list marker, "]<ref/>]"), so a second,
placeholder-free pass is the reference for the text; stats["placeholder_fallback"] counts any difference.
"""
import html, re, unicodedata
from collections import Counter
from dataclasses import dataclass

import xxhash

REF, OTHER = chr(0xE000), chr(0xE001)   # where a <ref> was / where a <references> list was (private-use characters)
MERGE_BELOW = 200                       # a paragraph shorter than this joins the previous chunk of its section ...
MERGE_MAX = 900                         # ... while that chunk stays within this many characters
SPLIT_ABOVE = 1500                      # longer paragraphs are split (list lines, sentences, then hard), at most this long
CUT_EVERY = {"\n": 16, " ": 6}          # content-defined cut: after a list line / sentence whose hash % k == 0
SECTION_MAX = 255                       # claim.section is VARCHAR(255)
# Headings of sections that hold sources, further reading, links and notes, not claims. "kirjallisuus" (literature)
# is deliberately absent: on these pages it heads prose about literature.
BOILERPLATE = {"lähteet", "lähde", "lähteitä", "viitteet", "huomautuksia", "huomautukset", "kirjallisuutta",
               "muuta kirjallisuutta", "muu kirjallisuus", "lähdekirjallisuus", "lähdekirjallisuutta", "lähteet ja kirjallisuus",
               "lähdeluettelo", "kirjalähteet", "verkkolähteet", "katso myös", "aiheesta muualla", "aiheesta muualla verkossa",
               "asiasta muualla", "muualla verkossa", "muualla aiheesta", "linkit", "linkkejä", "ulkoiset linkit",
               "see also", "external links", "references"}

_COLON_LINE = re.compile(r"(?m)^[ \t]*:+[ \t]*")
_REFS_LIST = re.compile(r"<references\b[^>]*/>|<references\b[^>]*>.*?</references\s*>", re.I | re.S)
# "<ref" not followed by "erences": catches <ref>, <REF ...>, and the malformed <refname="x"> the starter also caught
_REF_SELF = re.compile(r"<ref(?!erences)[^>]*/>", re.I)
_REF_PAIR = re.compile(r"<ref(?!erences)[^>]*>.*?</ref\s*>", re.I | re.S)
_FILE_LINK = re.compile(r"\[\[(?:Tiedosto|Kuva|File|Image|Luokka|Category):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]")
_HEADING = re.compile(r"^(=+)\s*(.*?)\s*=+$")
_SENT_END = re.compile(r"[.!?]" + REF + r"*(\s+)(?=\S)")    # candidate sentence end; _sentences checks the next char
_QUOTES = re.compile("'(?:" + REF + "*')*")                 # a run of apostrophes, maybe with REF placeholders between


@dataclass(frozen=True)
class Chunk:
    section: str
    text: str
    n_refs: int
    text_hash: int


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()


def text_hash(s: str) -> int:
    """xxh64 of the normalised text: fits BIGINT UNSIGNED; case and punctuation changes keep the hash."""
    return xxhash.xxh64_intdigest(norm(s).encode("utf-8"))


def drop_nested(text: str, start: str, end: str) -> str:
    """One linear pass with a stack: every matched start..end span is dropped (nesting included); a stray end with
    no open start stays as text. On balanced text that is exactly the starter's _drop_nested. An unclosed start
    drops itself through the end of its line, where the starter dropped the rest of the article; an unclosed
    infobox's parameter lines start with "|" and are dropped later by the line rule."""
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(start) + "|" + re.escape(end), text):
        if m.group() == start:
            stack.append(m.start())
        elif stack:
            spans.append((stack.pop(), m.end()))
    for s in stack:                                         # unclosed starts
        eol = text.find("\n", s)
        spans.append((s, eol if eol >= 0 else len(text)))
    out, pos = [], 0
    for s, e in sorted(spans):                              # emit the complement of the union of spans
        if s > pos:
            out.append(text[pos:s])
        pos = max(pos, e)
    out.append(text[pos:])
    return "".join(out)


def preprocess(wikitext: str, mark: bool) -> str:
    """Fixes 1, 3 and 5, applied before the starter's steps (also used by the oracle in eval/f3_check.py)."""
    t = _COLON_LINE.sub("", wikitext)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = _REFS_LIST.sub(OTHER if mark else "", t)
    t = _REF_SELF.sub(REF if mark else "", t)
    return _REF_PAIR.sub(REF if mark else "", t)


def _strip_quote_run(m: re.Match[str]) -> str:
    """The starter does replace("'''", "") then replace("''", "") on text whose refs are already gone, so
    apostrophes on both sides of a ref form one run. A run of k leaves one apostrophe iff k % 3 == 1."""
    run = m.group()
    return ("'" if run.count("'") % 3 == 1 else "") + REF * run.count(REF)


def _clean(wikitext: str, mark: bool) -> list[tuple[str, int, str]]:
    """The starter's clean() plus heading levels and fixes 1–5. mark=True keeps REF placeholders where citations were."""
    t = drop_nested(preprocess(wikitext, mark), "{{", "}}")
    t = drop_nested(t, "{|", "|}")
    t = _FILE_LINK.sub("", t)
    t = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _QUOTES.sub(_strip_quote_run, t.replace(OTHER, "")) if mark else t.replace("'''", "").replace("''", "")
    blocks = []
    for raw in t.split("\n"):
        line = raw.replace(REF, "").strip()                 # decide exactly as the starter would, without placeholders
        if not line:
            if REF in raw and blocks and blocks[-1][0] != "h":  # a citation on its own line backs the paragraph above
                k, lvl, prev = blocks[-1]; blocks[-1] = (k, lvl, prev + REF * raw.count(REF))
            continue
        kept = raw.strip()
        # the guard keeps the starter's heading regex from going cubic on long lines (it needs = at both ends anyway)
        heading = _HEADING.match(line) if line[0] == "=" and line[-1] == "=" else None
        if heading:
            blocks.append(("h", len(heading.group(1)), html.unescape(heading.group(2))))
        elif line[0] in "*#":
            blocks.append(("li", 0, html.unescape(kept.lstrip("*#: "))))
        elif line[0] not in "|!:;":
            blocks.append(("p", 0, html.unescape(kept)))
    return blocks


def clean_blocks(wikitext: str, stats: Counter | None = None) -> list[tuple[str, int, str]]:
    """(kind, level, text-with-REF): kind 'h' (level = number of '='), 'li' or 'p'."""
    marked, plain = _clean(wikitext, True), _clean(wikitext, False)
    if [(k, t.replace(REF, "").strip()) for k, _, t in marked] == [(k, t) for k, _, t in plain]:
        return marked
    if stats is not None:
        stats["placeholder_fallback"] += 1
    if [k for k, _, _ in marked] == [k for k, _, _ in plain]:
        return [(k, lvl, t + REF * m[2].count(REF)) for (k, lvl, t), m in zip(plain, marked)]
    return plain                                            # blocks do not align: exact text, citations not counted


def _heading_key(text: str) -> str:
    return text.replace(REF, "").strip().rstrip(":").strip().casefold()


def paragraphs(blocks: list[tuple[str, int, str]]) -> list[tuple[int, str, str]]:
    """(heading index, section, text-with-REF) per paragraph outside boilerplate sections; consecutive list items
    form one paragraph. The heading index keeps two sections with the same name apart."""
    out: list[tuple[int, str, str]] = []
    idx, section, prev_li = 0, "", False
    skip: int | None = None
    for kind, level, text in blocks:
        if kind == "h":
            idx += 1
            if skip is not None and level > skip:
                continue                                    # a subsection of a dropped section
            skip = level if _heading_key(text) in BOILERPLATE else None
            name = text.replace(REF, "").strip()
            section, prev_li = (name if len(name) <= SECTION_MAX else name[:SECTION_MAX - 1] + "…"), False
            continue
        if skip is not None:
            continue
        if kind == "li" and prev_li and out and out[-1][0] == idx:
            out[-1] = (idx, section, out[-1][2] + "\n" + text)
        else:
            out.append((idx, section, text))
        prev_li = kind == "li"
    return out


def _plain_len(s: str) -> int:
    return len(s.replace(REF, ""))


def _sentences(text: str) -> list[str]:
    sents, last = [], 0
    for m in _SENT_END.finditer(text):
        nxt = text[m.end(1)]
        if nxt.isupper() or nxt.isdigit() or nxt in "\"”«(":     # any script's capitals (Cyrillic pastes exist)
            sents.append(text[last:m.start(1)]); last = m.end(1)
    sents.append(text[last:])
    return sents


def _hard_split(text: str) -> list[str]:
    """Last resort for text with no list lines or sentence ends (vandal blobs reach 931k characters, and
    MariaDB TEXT holds 64 KB): cut at the last space before SPLIT_ABOVE, or at SPLIT_ABOVE if there is none."""
    out = []
    while _plain_len(text) > SPLIT_ABOVE:
        cut = text.rfind(" ", SPLIT_ABOVE // 2, SPLIT_ABOVE)
        cut = cut if cut > 0 else SPLIT_ABOVE
        out.append(text[:cut].strip()); text = text[cut:].strip()
    return out + [text] if text else out


def _pieces(text: str) -> list[str]:
    """A paragraph over SPLIT_ABOVE: split on list lines, else sentence ends, else hard. Pieces end at content-defined
    points (a part whose hash % k == 0), so inserting a list item or sentence moves at most the pieces around it,
    instead of shifting every later piece as greedy packing did; SPLIT_ABOVE is a hard cap."""
    if _plain_len(text) <= SPLIT_ABOVE:
        return [text]
    sep, parts = ("\n", text.split("\n")) if "\n" in text else (" ", _sentences(text))
    if len(parts) == 1:
        return _hard_split(text)
    out = [""]
    for part in (p for q in parts for p in _pieces(q)):     # any still-too-long part is split further
        if out[-1] and _plain_len(out[-1] + sep + part) > SPLIT_ABOVE:
            out.append(part)
        else:
            out[-1] = (out[-1] + sep + part).strip()
        if _plain_len(out[-1]) >= MERGE_BELOW and text_hash(part) % CUT_EVERY[sep] == 0:
            out.append("")
    return [p for p in out if p]


def chunks_from(paras: list[tuple[int, str, str]], stats: Counter | None = None) -> list[Chunk]:
    out: list[list] = []                                    # [heading index, section, text-with-REF]
    for idx, section, text in paras:
        for piece in _pieces(text):
            plain = piece.replace(REF, "").strip()
            prev = out[-1] if out and out[-1][0] == idx else None
            if not norm(plain):                             # no words, e.g. a line of bare citations
                if prev:
                    prev[2] += REF * piece.count(REF)       # its citations back the paragraph before it
                elif stats is not None:
                    stats["orphan_refs"] += piece.count(REF)
                continue
            if prev and len(plain) < MERGE_BELOW and _plain_len(prev[2]) + 1 + len(plain) <= MERGE_MAX:
                prev[2] += "\n" + piece
            else:
                out.append([idx, section, piece])
    tidy = lambda t: "\n".join(line.strip() for line in t.replace(REF, "").strip().split("\n"))
    return [Chunk(s, tidy(t), t.count(REF), text_hash(tidy(t))) for _, s, t in out]


def chunk_revision(wikitext: str | None, stats: Counter | None = None) -> list[Chunk]:
    return chunks_from(paragraphs(clean_blocks(wikitext or "", stats)), stats)
