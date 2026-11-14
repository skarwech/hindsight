"""Pure functions over one page's version list: real edit times and revert flags (plan Step 1).
Shared by every loader, so page_state, claim and layout get the same bumped times."""
from datetime import datetime, timedelta
from typing import Iterator, Sequence

FMT = "%Y-%m-%d %H:%M:%S.%f"
ONE_US = timedelta(microseconds=1)


def fmt(t: datetime) -> str:
    """Full timestamp string; MariaDB refuses a bare date for AS OF TIMESTAMP ? (F1.4-P2)."""
    return t.strftime(FMT)


def bumped_starts(times: Sequence[datetime]) -> list[datetime]:
    """times sorted ascending. Same-second edits get +1 µs each, order preserved: a history row with
    row_start = row_end is rejected (error 4112 on 12.3), so the bump is mandatory."""
    starts: list[datetime] = []
    for t in times:
        starts.append(t if not starts or t > starts[-1] else starts[-1] + ONE_US)
    return starts


def history_rows(starts: Sequence[datetime]) -> Iterator[tuple[datetime, datetime | None]]:
    """(row_start, row_end) per version; row_end None = the current version (INSERT without row_end)."""
    for i, s in enumerate(starts):
        yield s, (starts[i + 1] if i + 1 < len(starts) else None)


def flags(sha1s: Sequence[str | None], times: Sequence[datetime],
          max_span: timedelta = timedelta(hours=48)) -> tuple[list[bool], list[bool]]:
    """is_revert[i]: sha1 equals an earlier version's but not the previous one's (the starter's rule).
    A NULL sha1 (hidden text) never matches anything, as in the starter's SQL: neither the hidden
    version nor the version right after it can be a revert.
    undone[j]: j sits between the closest earlier identical version a and the reverting version b,
    unless a was itself undone (repeat of vandalism) or the span is longer than max_span."""
    n = len(sha1s)
    is_rev, undone, last_seen = [False] * n, [False] * n, {}
    for i, s in enumerate(sha1s):
        prev = sha1s[i - 1] if i else None
        if s is not None and prev is not None and s != prev and s in last_seen:
            is_rev[i] = True
            a = last_seen[s]
            if not undone[a] and times[i] - times[a] <= max_span:
                for j in range(a + 1, i):
                    undone[j] = True
        if s is not None:
            last_seen[s] = i
    return is_rev, undone
