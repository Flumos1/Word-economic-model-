"""SPF news-release calendar (official dates) shared by the ensemble layer.

The SPF medians become public at the news release, not at the response deadline.  The embedded
anchors are the official "News Release Date" values from spf-release-dates.txt (docs/86); for a
quarter without an anchor the conservative fallback is the last day of the middle month (the
release is in the 4th week before 2005Q1 and in the 2nd week after, so this fallback can be a few
days early for some old quarters; those quarters are additionally covered by the offline test set
in tests/test_spf_calendar.py).
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date

SPF_MIDDLE_MONTHS = {2, 5, 8, 11}

#: Exact official News Release Date anchors (spf-release-dates.txt, docs/86).
RELEASE_ANCHORS = {
    "1990Q3": date(1990, 8, 31),
    "1990Q4": date(1990, 11, 28),
    "1991Q1": date(1991, 2, 21),
    "2000Q1": date(2000, 2, 22),
    "2005Q1": date(2005, 2, 14),
    "2005Q2": date(2005, 5, 16),
    "2013Q4": date(2013, 11, 25),
    "2019Q1": date(2019, 3, 22),
    "2020Q1": date(2020, 2, 14),
    "2026Q1": date(2026, 3, 6),
    "2026Q2": date(2026, 5, 15),
    "2026Q3": date(2026, 8, 14),
}
#: Exact official True Deadline Date anchors (context only; availability uses the release).
DEADLINE_ANCHORS = {
    "1990Q3": date(1990, 8, 23),
    "2005Q1": date(2005, 2, 9),
    "2019Q1": date(2019, 3, 12),
    "2020Q1": date(2020, 2, 11),
    "2026Q1": date(2026, 3, 2),
}


def parse_quarter(label: str) -> tuple[int, int]:
    text = str(label).strip().upper()
    if len(text) < 6 or text[4] != "Q" or text[:4].isdigit() is False or text[5] not in "1234":
        raise ValueError(f"not a quarter label: {label!r}")
    return int(text[:4]), int(text[5])


def is_real_time_quarter(label: str) -> bool:
    """SPF values before 1990Q3 are not real-time (docs/86)."""
    year, quarter = parse_quarter(label)
    return (year, quarter) >= (1990, 3)


def _last_day_of_middle_month(label: str) -> date:
    year, quarter = parse_quarter(label)
    month = quarter * 3 - 1  # middle month of the quarter
    return date(year, month, _calendar.monthrange(year, month)[1])


def release_date(label: str) -> date:
    """Official news-release date; embedded anchors, else the conservative fallback."""
    text = str(label).strip().upper()
    parse_quarter(text)
    if text in RELEASE_ANCHORS:
        return RELEASE_ANCHORS[text]
    return _last_day_of_middle_month(text)


def available_at(origin, label: str) -> bool:
    """True when the median for ``label`` is public at the origin (origin >= release date)."""
    return origin >= release_date(label)
