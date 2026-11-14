"""Step 3 (F4): give each chunk a stable claim_id across revisions (the plan's resolver, Appendix A, extended).

Per revision, in order:
  (1)  an exact hash diff keeps unchanged paragraphs;
  (1b) a paragraph whose exact text a changed claim holds now (a move) or held before (a revert) keeps that claim's
       id; the current holder wins, then the past holder most similar to the new text;
  (3)  a paragraph whose exact text any deleted claim ever had revives it, most recently deleted first;
  (2)  an optimal one-to-one assignment on character similarity (>= t_edit) links rewrites; a paragraph that grew to
       twice its length or more while keeping its old text (growth containment) counts as similar;
  (2b) in the same assignment, a near-duplicate (similarity >= T_NEAR) of a claim deleted in the last LOOKBACK
       revisions revives it, so it is not handed to a weaker live match;
  (4)  optionally, a semantic pass links leftovers of the same section by embedding cosine (>= t_sem), also against
       claims deleted one revision earlier;
  then the rest is deleted or added.
Changes from the plan's code, each found by the F4 checks and reviews on the hero pages: the graveyard remembers every
text a claim ever had (the plan's dict knew only the last); (1b), (2b) and containment are new; revival runs before the
similarity pass; pairs below a threshold count as 0 in the assignment; duplicates revive in their original order.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal, NamedTuple

import numpy as np
from rapidfuzz import fuzz, process
from scipy.optimize import linear_sum_assignment

T_EDIT = 0.60          # plan Step 3
T_SEM = 0.88           # plan Step 3
T_NEAR = 0.90          # (2b): a re-added near-duplicate of a recently deleted claim
LOOKBACK = 10          # (2b): how many revisions back a deletion counts as recent
MIN_CONTAIN = 80       # containment counts only for paragraphs at least this long ...
CONTAIN = 0.90         # ... whose shorter text sits this completely inside the longer one


class Para(NamedTuple):
    hash: int
    text: str
    section: str


class Claim(NamedTuple):
    claim_id: int
    hash: int
    text: str
    section: str


class Event(NamedTuple):
    kind: Literal["add", "delete", "edit", "revive", "semantic"]
    claim_id: int
    sim: float | None
    rule: str = ""     # which step decided: move | revert | ratio | contain | exact | near | semantic


@dataclass
class Memory:
    """What the resolver remembers about one page's claims between revisions."""
    texts: dict[int, set[int]] = field(default_factory=dict)          # claim_id -> every hash it ever had
    dead: dict[int, tuple[int, int]] = field(default_factory=dict)    # claim_id -> (revision seq, position) of its deletion
    last: dict[int, str] = field(default_factory=dict)                # dead claim_id -> its text when deleted
    index: dict[int, set[int]] = field(default_factory=dict)          # hash -> claims that ever had it
    seq: int = 0

    def learn(self, cid: int, h: int) -> None:
        self.texts.setdefault(cid, set()).add(h); self.index.setdefault(h, set()).add(cid)

    def bury(self, c: Claim, pos: int) -> None:
        self.dead[c.claim_id] = (self.seq, pos); self.last[c.claim_id] = c.text

    def raise_(self, cid: int) -> None:
        self.dead.pop(cid, None); self.last.pop(cid, None)

    def revivable(self, h: int, used: set[int]) -> int | None:
        """The dead claim that had this text, most recently deleted first; same revision: earlier position first."""
        cands = [c for c in self.index.get(h, ()) if c in self.dead and c not in used]
        return max(cands, key=lambda c: (self.dead[c][0], -self.dead[c][1], -c)) if cands else None


def _assign(score: np.ndarray, threshold: float) -> list[tuple[int, int, float]]:
    """Optimal one-to-one links among pairs at or above the threshold. Pairs below it count as 0: otherwise two
    discarded 0.59 pairs (sum 1.18) beat one real 0.61 link, as in the plan's code."""
    rows, cols = linear_sum_assignment(-np.where(score >= threshold, score, 0.0))
    return [(int(a), int(b), float(score[a, b])) for a, b in zip(rows, cols) if score[a, b] >= threshold]


def similarity(olds: list[str], news: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Character similarity (rapidfuzz ratio), raised for growth: when the new text is at least twice the old one
    (>= MIN_CONTAIN chars) and the old text sits >= CONTAIN inside it, the pair scores 0.9 * partial + 0.05 * ratio.
    Growth only: in the golden set an expanded paragraph was judged the same claim every time, a paragraph cut to under
    half (the reverse containment) almost never. Returns (score, raised-by-containment)."""
    score = process.cdist(olds, news, scorer=fuzz.ratio) / 100
    contained = np.zeros(score.shape, dtype=bool)
    lo, ln = np.array([len(t) for t in olds])[:, None], np.array([len(t) for t in news])[None, :]
    for a, b in zip(*np.nonzero((lo >= MIN_CONTAIN) & (ln >= 2 * lo) & (score < CONTAIN))):
        p = fuzz.partial_ratio(olds[a], news[b]) / 100
        if p >= CONTAIN and 0.9 * p + 0.05 * score[a, b] > score[a, b]:
            score[a, b] = 0.9 * p + 0.05 * score[a, b]; contained[a, b] = True
    return score, contained


def resolve(prev: list[Claim], cur: list[Para], mem: Memory, new_id: Callable[[], int], t_edit: float = T_EDIT,
            vectors: dict[int, np.ndarray] | None = None, recent: list[Claim] | None = None,
            t_sem: float = T_SEM) -> tuple[list[int], list[Event]]:
    """prev: the claims of revision i-1 in order; cur: the paragraphs of revision i; mem is updated in place.
    vectors (hash -> unit vector) turns on the semantic pass; recent are the claims deleted in revision i-1, which it
    may also link. Returns (claim id per paragraph, events)."""
    mem.seq += 1
    ids: list[int | None] = [None] * len(cur)
    events: list[Event] = []
    sm = SequenceMatcher(None, [p.hash for p in prev], [c.hash for c in cur], autojunk=False)
    gone: list[int] = []; fresh: list[int] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ids[j1:j2] = [p.claim_id for p in prev[i1:i2]]
        else:
            gone += range(i1, i2); fresh += range(j1, j2)
    holders: dict[int, list[int]] = {}
    for o in gone:
        holders.setdefault(prev[o].hash, []).append(o)
    taken: set[int] = set()
    for n in fresh:                                        # (1b) a move: the claim holding this exact text now
        o = next((o for o in holders.get(cur[n].hash, []) if o not in taken), None)
        if o is not None:
            ids[n] = prev[o].claim_id; taken.add(o); events.append(Event("edit", ids[n], 1.0, "move"))
    for n in fresh:                                        # (1b) a revert: a changed claim that held this text before
        if ids[n] is None:
            cands = [o for o in gone if o not in taken and cur[n].hash in mem.texts.get(prev[o].claim_id, ())]
            if cands:
                o = max(cands, key=lambda o: (fuzz.ratio(prev[o].text, cur[n].text), -o))
                ids[n] = prev[o].claim_id; taken.add(o); events.append(Event("edit", ids[n], 1.0, "revert"))
    gone = [o for o in gone if o not in taken]
    used = {i for i in ids if i is not None}
    for n in fresh:                                        # (3) revival by exact text, before (2): an exact text
        if ids[n] is None:                                 # beats a look-alike claim created during vandalism
            c = mem.revivable(cur[n].hash, used)
            if c is not None:
                ids[n] = c; used.add(c); mem.raise_(c); events.append(Event("revive", c, None, "exact"))
    left = [n for n in fresh if ids[n] is None]
    recent_dead = sorted(c for c, (seq, _) in mem.dead.items() if seq >= mem.seq - LOOKBACK and c not in used)
    if (gone or recent_dead) and left:                     # (2) rewrites of live claims and (2b) near-duplicates of
        texts = [cur[n].text for n in left]                # recently deleted ones compete in one assignment
        score, contained = similarity([prev[o].text for o in gone], texts) if gone else (np.zeros((0, len(left))), np.zeros((0, len(left)), bool))
        if recent_dead:
            near = process.cdist([mem.last[c] for c in recent_dead], texts, scorer=fuzz.ratio) / 100
            score = np.vstack([score, np.where(near >= T_NEAR, near, 0.0)])
        for a, b, s in _assign(score, t_edit):
            if a < len(gone):
                ids[left[b]] = prev[gone[a]].claim_id; used.add(ids[left[b]])
                events.append(Event("edit", ids[left[b]], s, "contain" if contained[a, b] else "ratio"))
            else:
                c = recent_dead[a - len(gone)]; ids[left[b]] = c; used.add(c); mem.raise_(c)
                events.append(Event("revive", c, s, "near"))
    gone = [o for o in gone if prev[o].claim_id not in used]
    if vectors is not None:                                # (4) semantic pass, same section only
        olds = [prev[o] for o in gone] + [c for c in (recent or []) if c.claim_id not in used and c.claim_id in mem.dead]
        news = [n for n in fresh if ids[n] is None]
        if olds and news:
            score = np.full((len(olds), len(news)), -1.0)
            for a, c in enumerate(olds):
                for b, n in enumerate(news):
                    if c.section == cur[n].section and c.hash in vectors and cur[n].hash in vectors:
                        score[a, b] = float(vectors[c.hash] @ vectors[cur[n].hash])
            for a, b, s in _assign(score, t_sem):
                c = olds[a]; ids[news[b]] = c.claim_id; used.add(c.claim_id); mem.raise_(c.claim_id)
                events.append(Event("semantic", c.claim_id, s, "semantic"))
            gone = [o for o in gone if prev[o].claim_id not in used]
    for o in gone:                                         # no successor: deleted
        mem.bury(prev[o], o); events.append(Event("delete", prev[o].claim_id, None))
    for n in fresh:                                        # no predecessor: brand new
        if ids[n] is None:
            ids[n] = new_id(); events.append(Event("add", ids[n], None))
    if None in ids or len(set(ids)) != len(ids):
        raise RuntimeError("resolver produced a missing or duplicate claim id")
    for cid, p in zip(ids, cur, strict=True):
        mem.learn(cid, p.hash)                             # type: ignore[arg-type]
    return ids, events  # type: ignore[return-value]
