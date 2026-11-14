"""Golden cases for chunker.py (plan Step 2 / F3, plus the fixes from the F3 review).  cd pipeline && ../.venv/bin/python -m pytest -q"""
import random

from chunker import MERGE_MAX, REF, SECTION_MAX, SPLIT_ABOVE, chunk_revision, clean_blocks, drop_nested, norm, text_hash

LONG = "Talvisota alkoi marraskuussa 1939 ja päättyi maaliskuussa 1940 Moskovan rauhaan, jossa Suomi menetti alueita. "


def starter_drop_nested(text: str, start: str, end: str) -> str:     # the starter's _drop_nested, verbatim logic
    out, depth, i = [], 0, 0
    while i < len(text):
        if text.startswith(start, i):
            depth += 1; i += len(start)
        elif depth and text.startswith(end, i):
            depth -= 1; i += len(end)
        else:
            if not depth:
                out.append(text[i])
            i += 1
    return "".join(out)


def test_starters_own_clean_case() -> None:     # the starter's test_history.py example, kinds and texts unchanged
    blocks = clean_blocks("{{Infobox|a={{b}}}}'''Talvisota''' oli [[sota|sotaa]] [[Suomi]] "
                          "vastaan.<ref>lähde</ref>\n== Tausta ==\n* kohta\n[[Luokka:Sodat]]")
    assert [(k, t.replace(REF, "")) for k, _, t in blocks] == [("p", "Talvisota oli sotaa Suomi vastaan."), ("h", "Tausta"), ("li", "kohta")]


def starter_ends_balanced(text: str, start: str, end: str) -> bool:
    depth, i = 0, 0
    while i < len(text):
        if text.startswith(start, i):
            depth += 1; i += len(start)
        elif depth and text.startswith(end, i):
            depth -= 1; i += len(end)
        else:
            i += 1
    return depth == 0


def test_drop_nested_matches_starter_when_balanced() -> None:
    rnd = random.Random(1)
    balanced = 0
    for _ in range(20000):
        s = "".join(rnd.choice("{}|ab \n") for _ in range(rnd.randint(0, 30)))
        for a, b in (("{{", "}}"), ("{|", "|}")):
            if starter_ends_balanced(s, a, b):                       # fix 2 only changes the unclosed case
                balanced += 1
                assert drop_nested(s, a, b) == starter_drop_nested(s, a, b), (s, a)
    assert balanced > 10000


def test_unclosed_template_drops_only_itself() -> None:   # fix 2: the starter lost the rest of the article
    text = "{{Infobox\n| nimi = X\n" + LONG * 2 + "\n\n== Tausta ==\n" + LONG * 2
    c = chunk_revision(text)
    assert [x.section for x in c] == ["", "Tausta"] and "nimi" not in c[0].text
    assert drop_nested("a {{b {{c}} d\nseuraava {{x}} rivi", "{{", "}}") == "a \nseuraava  rivi"


def test_apostrophes_across_a_ref_behave_as_one_run() -> None:   # found by F3.7 on rev 5543883
    for k1 in range(4):
        for k2 in range(4):
            raw = "Alku" + "'" * k1 + "<ref>x</ref>" + "'" * k2 + " loppu."
            starter = ("Alku" + "'" * (k1 + k2) + " loppu.").replace("'''", "").replace("''", "")
            assert clean_blocks(raw)[0][2].replace(REF, "") == starter, (k1, k2)


def test_refs_counted_per_paragraph_and_references_list_ignored() -> None:
    text = ("Eka kappale.<ref>a</ref><ref name=x/> " + LONG * 2 + "\n\nToka kappale ilman lähteitä. " + LONG * 2
            + "\n\n== Lähteet ==\n<references/>\n")
    c = chunk_revision(text)
    assert [x.n_refs for x in c] == [2, 0] and "<" not in c[0].text


def test_citation_on_its_own_line_counts_for_the_paragraph_above() -> None:   # found by F3.5
    text = LONG * 2 + "\n<ref>Lähde 1</ref>\n\n" + LONG.replace("1939", "1940") * 2 + "\n\n<ref name=b/>\n"
    assert [x.n_refs for x in chunk_revision(text)] == [1, 1]


def test_uppercase_and_slash_named_refs() -> None:              # fix 1
    text = ('Alku.<REF>Lähde, s. 3</REF> ' + LONG * 2 + '\n\nToinen.<ref name="friman/92"/> ' + LONG * 2
            + "\n\nKolmas kappale säilyy kokonaan.<ref>y</ref> " + LONG * 2)
    c = chunk_revision(text)
    assert [x.n_refs for x in c] == [1, 1, 1] and "Lähde, s. 3" not in c[0].text and c[2].text.startswith("Kolmas")


def test_entities_decoded_and_colon_lines_kept() -> None:       # fixes 3 and 4
    c = chunk_revision(":Suomen kansa on ottanut kohtalonsa omiin käsiinsä.&nbsp;23&nbsp;000 ihmistä &ndash; " + LONG * 2)
    assert c and c[0].text.startswith("Suomen kansa") and "nbsp" not in norm(c[0].text) and "23 000" in norm(c[0].text)


def test_boilerplate_sections_and_subsections_dropped_but_literature_kept() -> None:
    text = (LONG + "\n== Historia ==\n" + LONG + "\n== Lähteet ==\n=== Viitteet ===\nKirja 1999.\n"
            "== Aiheesta muualla: ==\nLinkki.\n== Kirjallisuus ==\n" + LONG + "\n== Kirjallisuutta ==\nKirja 2001.\n")
    assert [x.section for x in chunk_revision(text)] == ["", "Historia", "Kirjallisuus"]


def test_short_paragraph_merges_only_within_the_same_heading() -> None:
    text = LONG * 2 + "\n\nLyhyt lisäys.\n\n" + LONG * 2 + "\n\n== Tausta ==\n" + LONG * 2 + "\n== Muu ==\n== Tausta ==\nLyhyt."
    c = chunk_revision(text)
    assert len(c) == 4 and c[0].text.endswith("Lyhyt lisäys.") and c[3].text == "Lyhyt."
    assert all(len(x.text) <= MERGE_MAX or "\n" not in x.text for x in c)


def test_long_paragraph_split_on_sentences_with_refs_kept() -> None:
    para = "".join(f"Lause {i} kertoo asiasta {i} pitkästi ja perusteellisesti, jotta kappale kasvaa.<ref>l{i}</ref> " for i in range(60))
    c = chunk_revision(para)
    assert len(c) > 1 and all(len(x.text) <= SPLIT_ABOVE for x in c) and sum(x.n_refs for x in c) == 60
    joined = norm(" ".join(x.text for x in c))
    assert all(norm(f"Lause {i} kertoo asiasta {i}") in joined for i in range(60))      # no sentence lost


def test_long_list_split_on_items_and_stable_under_insert() -> None:
    items = [f"* Henkilö {i} (1900–1990), ammatiltaan jotakin" for i in range(120)]
    before = chunk_revision("\n".join(items))
    assert len(before) > 1 and all(len(x.text) <= SPLIT_ABOVE for x in before)
    assert sum(x.text.count("\n") + 1 for x in before) == 120                         # every item kept, none cut
    after = chunk_revision("\n".join(items[:3] + ["* Uusi henkilö (1950–2020), lisätty"] + items[3:]))
    assert len({x.text_hash for x in after} - {x.text_hash for x in before}) <= 2      # greedy packing changed them all


def test_vandal_blob_without_sentences_is_bounded() -> None:
    for blob in ("kekkonen " * 20000, "uli" * 30000):
        c = chunk_revision(blob)
        assert c and all(len(x.text) <= SPLIT_ABOVE for x in c)
        assert sum(len(x.text) for x in c) >= len(blob.strip()) - len(c)       # nothing lost but the cut spaces


def test_long_section_name_capped() -> None:
    c = chunk_revision("== " + "x" * 900 + " ==\n" + LONG * 2)
    assert len(c[0].section) == SECTION_MAX


def test_hash_ignores_case_and_punctuation_but_not_letters() -> None:
    assert text_hash("Talvisota alkoi 1939.") == text_hash("talvisota, alkoi 1939")
    assert text_hash("Säkkijärvi") != text_hash("Sakkijarvi")
    assert 0 <= text_hash("x") < 2 ** 64


def test_edit_in_one_paragraph_changes_one_hash() -> None:
    paras = [LONG.replace("1939", str(1900 + i)) * 2 for i in range(8)]
    before = [x.text_hash for x in chunk_revision("\n\n".join(paras))]
    paras[3] = paras[3].replace("alueita", "paljon alueita")
    after = [x.text_hash for x in chunk_revision("\n\n".join(paras))]
    assert len(before) == len(after) == 8 and sum(a != b for a, b in zip(before, after)) == 1


def test_deterministic() -> None:
    text = (LONG * 3 + "\n\n") * 5
    assert chunk_revision(text) == chunk_revision(text)


def test_malformed_ref_tag_is_still_a_citation() -> None:      # <refname="x"> found by F3.7 on rev 19709326
    c = chunk_revision('Teksti päättyy tähän.<refname="siltala58">Siltala 1985, s. 58–61.</ref> ' + LONG * 2)
    assert c[0].n_refs == 1 and "Siltala" not in c[0].text


def test_punctuation_line_citation_counts_for_the_paragraph_above() -> None:
    assert [x.n_refs for x in chunk_revision(LONG * 2 + "\n.<ref>x</ref>\n")] == [1]
