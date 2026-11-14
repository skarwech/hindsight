"""Golden cases for revisions.py (plan F2.6 plus the bump and NULL rules).  .venv/bin/python -m pytest pipeline -q"""
from datetime import datetime, timedelta

from revisions import bumped_starts, flags, history_rows

def T(*hours: int) -> list[datetime]:
    return [datetime(2020, 1, 1) + timedelta(hours=x) for x in hours]


def test_basic_revert() -> None:
    assert flags(list("ABCA"), T(0, 1, 2, 3)) == ([False, False, False, True], [False, True, True, False])


def test_repeated_vandalism_flags_no_good_edit() -> None:
    assert flags(list("AVAVA"), T(0, 1, 2, 3, 4))[1] == [False, True, False, True, False]


def test_blank_repeated_after_good_edit_keeps_it() -> None:
    assert flags(list("AVAXV"), T(0, 1, 2, 3, 4))[1] == [False, True, False, False, False]


def test_long_range_revert_is_not_cleanup() -> None:
    assert flags(list("ABCA"), T(0, 10, 20, 100))[1] == [False] * 4


def test_consecutive_duplicates_are_not_reverts() -> None:
    assert flags(list("AABB"), T(0, 1, 2, 3)) == ([False] * 4, [False] * 4)


def test_null_sha1_never_matches() -> None:      # the starter's SQL: NULL <> x is NULL, so not a revert
    assert flags(["A", None, "A", None, None], T(0, 1, 2, 3, 4))[0] == [False] * 5
    assert flags(["A", "B", None, "A"], T(0, 1, 2, 3))[0] == [False] * 4
    assert flags(["A", None, "B", "A"], T(0, 1, 2, 3))[0] == [False, False, False, True]


def test_same_second_bump_keeps_order_and_positive_periods() -> None:
    t = datetime(2020, 1, 1)
    starts = bumped_starts([t, t, t, t + timedelta(seconds=1)])
    assert starts == [t, t + timedelta(microseconds=1), t + timedelta(microseconds=2), t + timedelta(seconds=1)]
    rows = list(history_rows(starts))
    assert all(e is None or s < e for s, e in rows) and rows[-1][1] is None
    assert [e for _, e in rows[:-1]] == starts[1:]
