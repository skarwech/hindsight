"""The plan's nine resolver cases (Appendix A), its 400-edit simulation, and the duplicate and semantic additions.
    cd pipeline && ../.venv/bin/python -m pytest -q test_resolver.py"""
import random, re

import numpy as np
from rapidfuzz.fuzz import ratio

from chunker import text_hash
from resolver import Claim, Memory, Para, resolve

P = [
    "The Finnish Civil War was fought in 1918 between the Reds and the Whites after Finland declared independence from Russia in December 1917.",
    "The Winter War began in November 1939 when the Soviet Union attacked Finland and ended with the Moscow Peace Treaty in March 1940.",
    "The Continuation War lasted from 1941 to 1944 and ended with an armistice signed in Moscow on 19 September 1944 between Finland and the Allies.",
    "Urho Kekkonen served as President of Finland from 1956 to 1982 and shaped the country's foreign policy during the Cold War era.",
    "Finland joined the European Union in 1995 and adopted the euro as its currency in 1999 together with ten other member states.",
    "The Aland Islands are an autonomous region of Finland with a Swedish-speaking population and a special status under international law.",
]
REWRITE = "In late November 1939 the Soviet Union invaded Finland, which started the Winter War; it concluded in March 1940 with the Treaty of Moscow."


class Run:
    """Feeds revisions through resolve() the way pipeline/lineage.py does."""
    def __init__(self, vectors: dict | None = None) -> None:
        self.n, self.mem, self.cur, self.recent, self.vectors = 0, Memory(), [], [], vectors

    def nid(self) -> int:
        self.n += 1; return self.n

    def step(self, paras: list[str], section: str = "") -> tuple[list[int], list]:
        cur = [Para(text_hash(p), p, section) for p in paras]
        ids, ev = resolve(self.cur, cur, self.mem, self.nid, vectors=self.vectors, recent=self.recent)
        assert len(ids) == len(cur) and len(set(ids)) == len(ids), "id invariants"
        gone = {e.claim_id for e in ev if e.kind == "delete"}
        self.recent = [c for c in self.cur if c.claim_id in gone]
        self.cur = [Claim(ids[k], cur[k].hash, cur[k].text, section) for k in range(len(cur))]
        return ids, ev


kinds = lambda ev: sorted(e.kind for e in ev)


def test_insert() -> None:
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[1], P[3], P[2]])
    assert b == [a[0], a[1], 4, a[2]] and kinds(ev) == ["add"]


def test_delete() -> None:
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[2]])
    assert b == [a[0], a[2]] and kinds(ev) == ["delete"] and ev[0].claim_id == a[1]


def test_typo() -> None:
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[1].replace("November", "Novembre"), P[2]])
    assert b == a and kinds(ev) == ["edit"] and ev[0].sim >= 0.9


def test_move() -> None:
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[2], P[1]])
    assert b == [a[0], a[2], a[1]] and set(kinds(ev)) == {"edit"} and all(e.sim == 1.0 for e in ev)


def test_rewrite_needs_the_semantic_pass() -> None:
    assert ratio(P[1], REWRITE) / 100 < 0.60                          # below the character threshold (0.57 in the plan)
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], REWRITE, P[2]])
    assert kinds(ev) == ["add", "delete"]                             # pass 1 alone: delete plus add
    v = {text_hash(P[1]): np.array([1.0, 0.0]), text_hash(REWRITE): np.array([0.95, 0.312])}   # cosine 0.95
    r = Run(vectors=v); a, _ = r.step(P[:3]); b, ev = r.step([P[0], REWRITE, P[2]])
    assert b == a and kinds(ev) == ["semantic"]                       # after the semantic pass: same id


def test_semantic_pass_reaches_one_revision_back_and_respects_sections() -> None:
    v = {text_hash(P[1]): np.array([1.0, 0.0]), text_hash(REWRITE): np.array([0.95, 0.312])}
    r = Run(vectors=v); a, _ = r.step(P[:3]); r.step([P[0], P[2]]); b, ev = r.step([P[0], REWRITE, P[2]])
    assert b[1] == a[1] and kinds(ev) == ["semantic"] and not r.mem.dead  # deleted at i-1, linked at i
    r = Run(vectors=v); a, _ = r.step(P[:3], "A"); b, ev = r.step([P[0], REWRITE, P[2]], "B")
    assert "semantic" not in kinds(ev)                                # a different section is never linked


def test_split() -> None:
    cut = P[1].index(" and ended")
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], P[1][:cut] + ".", "It " + P[1][cut + 1:], P[2]])
    assert kinds(ev) == ["add", "edit"] and b[0] == a[0] and b[3] == a[2]


def test_merge() -> None:
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0] + " " + P[1], P[2]])
    assert kinds(ev) == ["delete", "edit"] and b[1] == a[2] and b[0] in (a[0], a[1])


def test_blank_and_restore() -> None:
    r = Run(); a, _ = r.step(P); _, e1 = r.step(["Page blanked."]); c, e2 = r.step(P)
    assert c == a and kinds(e2).count("revive") == len(P) and "add" not in kinds(e2) and kinds(e1).count("delete") == len(P)


def test_blank_and_restore_with_a_duplicate_paragraph() -> None:   # the plan's grave dict lost one of the two
    page = [P[0], P[1], P[0], P[2]]
    r = Run(); a, _ = r.step(page); r.step(["Page blanked."]); c, e2 = r.step(page)
    assert c == a and kinds(e2).count("revive") == 4 and "add" not in kinds(e2)


def test_unrelated_swap() -> None:
    X = "Helsinki hosted the Summer Olympic Games in 1952, drawing athletes from sixty-nine countries to the new stadium."
    Y = "Nokia began as a paper mill in 1865 before turning to rubber, cables and finally mobile telephones in the late twentieth century."
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], X, Y])
    assert b[0] == a[0] and b[1] not in a and b[2] not in a and kinds(ev) == ["add", "add", "delete", "delete"]


def test_sub_threshold_pairs_do_not_block_a_real_link() -> None:   # found by the F4 pool: unlinked pairs >= 0.60
    import numpy as np
    from resolver import _assign
    score = np.array([[0.59, 0.61], [0.00, 0.59]])                # gone A, B x fresh X, Y; A-Y is the only real link
    assert _assign(score, 0.60) == [(0, 1, 0.61)]


def test_replay_is_deterministic() -> None:
    def replay() -> list:
        r = Run(); return [r.step(p)[0] for p in (P[:3], [P[0], P[2]], P[:3], [P[0], "Page blanked."], P[:3])]
    assert replay() == replay()


def test_simulation_400_edits_keeps_ids() -> None:            # the plan's simulation: 0 id mismatches
    random.seed(7)
    vocab = [w for p in P for w in re.findall(r"[A-Za-z]+", p)] + ["treaty", "army", "border", "senate", "harbour", "railway"]
    para = lambda: " ".join(random.choice(vocab) for _ in range(random.randint(18, 28))).capitalize() + "."
    def typo(s: str) -> str:
        w = s.split(); i = random.randrange(len(w)); w[i] += random.choice("sx"); return " ".join(w)
    def light(s: str) -> str:
        w = s.split()
        for _ in range(max(1, len(w) // 10)):
            w[random.randrange(len(w))] = random.choice(vocab)
        return " ".join(w)
    doc = [(i, para()) for i in range(30)]; nextg = 30
    r = Run(); ids, _ = r.step([t for _, t in doc]); truth = {g: ids[k] for k, (g, _) in enumerate(doc)}
    bad = total = 0
    for _ in range(400):
        op = random.choice(["typo", "light", "move", "insert", "delete"]); k = random.randrange(len(doc))
        if op == "typo": doc[k] = (doc[k][0], typo(doc[k][1]))
        elif op == "light": doc[k] = (doc[k][0], light(doc[k][1]))
        elif op == "move" and len(doc) > 1: x = doc.pop(k); doc.insert(random.randrange(len(doc) + 1), x)
        elif op == "insert": doc.insert(random.randrange(len(doc) + 1), (nextg, para())); nextg += 1
        elif op == "delete" and len(doc) > 3: doc.pop(k)
        ids, _ = r.step([t for _, t in doc])
        for (g, _), cid in zip(doc, ids):
            if g in truth:
                total += 1; bad += truth[g] != cid
            else:
                truth[g] = cid
    assert total > 10000 and bad == 0, (bad, total)


def test_restore_after_edit_then_delete_revives_the_claim() -> None:   # the plan's graveyard knew only the last text
    r = Run(); a, _ = r.step(P[:3]); r.step([P[0], P[1].replace("1939", "1939 (vandal)"), P[2]]); r.step([P[0], P[2]])
    c, ev = r.step(P[:3])
    assert c == a and kinds(ev) == ["revive"]


def test_revert_to_an_earlier_own_text_keeps_the_id() -> None:
    X = "Completely different vandal text about football results and nothing else at all whatsoever."
    r = Run(); a, _ = r.step(P[:3]); r.step([P[0], X, P[2]]); c, ev = r.step(P[:3])
    assert c[0] == a[0] and c[2] == a[2] and c[1] == a[1] and "add" not in kinds(ev)


def test_most_recently_deleted_claim_is_revived() -> None:
    r = Run(); a, _ = r.step([P[0], P[1]]); r.step([P[0]]); b, _ = r.step([P[0], P[1]])   # claim a[1] dies, revives
    r.step([P[0]]); r.step([P[0], P[3]]); d, _ = r.step([P[0], P[1], P[3]])
    assert d[1] == b[1]


def test_move_goes_to_the_claim_holding_the_text_now() -> None:   # code review repro: H held by c2, X by c1
    H, X = P[3], P[4]
    r = Run(); a, _ = r.step([X, H]); r.step([H, X])               # swap: both moves
    Y1, Y2 = P[0], P[1]
    b, _ = r.step([Y1, Y2, X.replace("1995", "1996"), H])
    assert b[3] == a[1] and b[2] == a[0]


def test_near_duplicate_re_add_revives_a_recently_deleted_claim() -> None:
    r = Run(); a, _ = r.step(P[:3]); r.step([P[0], P[2]]); r.step([P[0], P[2], P[4]])
    c, ev = r.step([P[0], P[1].replace("1940.", "1940 in Moscow."), P[2], P[4]])
    assert c[1] == a[1] and ("revive" in kinds(ev))


def test_paragraph_expanded_past_twice_its_length_keeps_its_id() -> None:
    grown = P[1] + " " + " ".join(P[3:6]) + " The war's legacy shaped Finnish defence policy for decades."
    from rapidfuzz.fuzz import ratio
    assert ratio(P[1], grown) / 100 < 0.60
    r = Run(); a, _ = r.step(P[:3]); b, ev = r.step([P[0], grown, P[2]])
    assert b == a and kinds(ev) == ["edit"]


def test_short_text_inside_an_unrelated_long_one_is_not_linked() -> None:
    short = "Lähteet:"                                            # too short for containment
    r = Run(); a, _ = r.step([short, P[1]]); b, ev = r.step([P[1] + " " + P[2] + " " + P[3]])
    assert b[0] == a[1] and kinds(ev).count("delete") == 1


def test_paragraph_cut_to_a_fragment_is_not_linked_by_containment() -> None:   # the golden set: shrinks judged different
    long_para = P[1] + " " + " ".join(P[3:6]) + " The war's legacy shaped Finnish defence policy for decades."
    r = Run(); a, _ = r.step([P[0], long_para, P[2]]); b, ev = r.step([P[0], P[1], P[2]])
    assert b[1] != a[1] and kinds(ev) == ["add", "delete"]


X_NEAR = P[1].replace("1940.", "1940 in Moscow.")                          # a near-duplicate of P[1]
Z_LIVE = ("The Winter War started in late 1939 when the Red Army attacked Finland, and it ended with the Moscow "
          "Peace Treaty in March 1940.")                                     # a weaker look-alike of it, still live


def test_near_duplicate_beats_a_weaker_live_match() -> None:   # code re-review: (2) and (2b) must compete in one assignment
    live, dead = ratio(Z_LIVE, X_NEAR) / 100, ratio(P[1], X_NEAR) / 100
    assert 0.60 <= live < 0.90 <= dead, (live, dead)                      # the scenario: both would link on their own
    r = Run(); a, _ = r.step([P[0], P[1], Z_LIVE, P[3]])
    r.step([P[0], Z_LIVE, P[3]])                                          # P[1] deleted
    b, ev = r.step([P[0], X_NEAR, P[3]])                                  # Z removed while P[1]'s near-duplicate appears
    assert b[1] == a[1] and [(e.kind, e.rule) for e in ev if e.kind != "delete"] == [("revive", "near")]
    assert [e.claim_id for e in ev if e.kind == "delete"] == [a[2]]       # the weaker live match is deleted, not linked


def test_near_revival_window_is_exactly_lookback() -> None:
    from resolver import LOOKBACK

    def revives_after(gap: int) -> bool:                                  # gap = revisions from the deletion to the re-add
        r = Run(); a, _ = r.step([P[0], P[1]]); r.step([P[0]])
        for _ in range(gap - 1):
            r.step([P[0]])
        b, ev = r.step([P[0], X_NEAR])
        return b[1] == a[1] and [e.rule for e in ev] == ["near"]
    assert revives_after(1) and revives_after(LOOKBACK) and not revives_after(LOOKBACK + 1)
