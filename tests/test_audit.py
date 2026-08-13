"""Date-audit tests. Deterministic, no API key, no network.

These checks are code rather than a model call precisely so they can be pinned
like this: a model asked to do arithmetic on dates will occasionally report an
overlap that isn't there, and a false "impossible date" flag on a real CV would
send the user chasing a problem that doesn't exist.
"""

from src.contracts import Role
from src.extract import audit_dates, months_between, parse_month, parse_range

TODAY = "2026-08"


def role(org, start, end, title="Engineer"):
    return Role(title=title, org=org, start=start, end=end)


# --- date parsing ------------------------------------------------------------


def test_parse_month_abbreviations():
    assert parse_month("Feb 2026") == "2026-02"
    assert parse_month("Sep 2024") == "2024-09"
    assert parse_month("December 2023") == "2023-12"
    assert parse_month("Jan. 2025") == "2025-01"


def test_parse_month_present_is_none():
    # None means "current", which must stay distinct from "unknown".
    for text in ("Present", "present", "Current", "now", "Ongoing"):
        assert parse_month(text) is None


def test_parse_month_bare_year_defaults_to_january():
    assert parse_month("2022") == "2022-01"


def test_parse_range_handles_en_dash():
    # The real CV uses an en dash, not a hyphen.
    assert parse_range("Feb 2026 – Jun 2026") == ("2026-02", "2026-06")
    assert parse_range("Mar 2020 - Jun 2023") == ("2020-03", "2023-06")
    assert parse_range("Jan 2025 — Present") == ("2025-01", None)


def test_months_between():
    assert months_between("2024-01", "2024-01") == 0
    assert months_between("2023-12", "2024-03") == 3
    assert months_between("2020-03", "2023-06") == 39


# --- overlap detection -------------------------------------------------------


def test_no_flags_for_clean_sequential_history():
    roles = [
        role("A", "2020-01", "2022-01"),
        role("B", "2022-01", "2024-01"),
        role("C", "2024-01", None),
    ]
    assert audit_dates(roles, today=TODAY) == []


def test_adjacent_roles_do_not_overlap():
    # B starting exactly when A ends is a clean handover, not an overlap.
    roles = [role("A", "2023-12", "2025-01"), role("B", "2025-01", "2026-01")]
    assert [f for f in audit_dates(roles, today=TODAY) if f.kind == "date_overlap"] == []


def test_real_overlap_is_flagged():
    # The actual case in this CV: an Accenture externship inside the Texcio tenure.
    roles = [
        role("Texcio", "2023-12", "2025-01"),
        role("Accenture", "2024-09", "2024-11"),
    ]
    flags = [f for f in audit_dates(roles, today=TODAY) if f.kind == "date_overlap"]
    assert len(flags) == 1
    assert "Texcio" in flags[0].detail and "Accenture" in flags[0].detail
    assert flags[0].severity == "medium"  # 2 months


def test_long_overlap_is_high_severity():
    roles = [role("A", "2022-01", "2024-01"), role("B", "2022-06", "2023-12")]
    flags = [f for f in audit_dates(roles, today=TODAY) if f.kind == "date_overlap"]
    assert flags and flags[0].severity == "high"


# --- impossible dates --------------------------------------------------------


def test_end_before_start_is_flagged_high():
    flags = audit_dates([role("Backwards", "2024-06", "2023-01")], today=TODAY)
    kinds = [f.kind for f in flags]
    assert "impossible_date" in kinds
    assert all(f.severity == "high" for f in flags if f.kind == "impossible_date")


def test_future_start_is_flagged():
    flags = audit_dates([role("Future", "2027-01", None)], today=TODAY)
    assert any(f.kind == "impossible_date" for f in flags)


# --- gaps --------------------------------------------------------------------


def test_gap_is_flagged():
    roles = [role("A", "2020-03", "2023-06"), role("B", "2023-12", "2025-01")]
    gaps = [f for f in audit_dates(roles, today=TODAY) if f.kind == "employment_gap"]
    assert len(gaps) == 1
    assert "6-month" in gaps[0].detail


def test_short_gap_is_ignored():
    roles = [role("A", "2024-01", "2025-01"), role("B", "2025-03", None)]
    assert [f for f in audit_dates(roles, today=TODAY) if f.kind == "employment_gap"] == []


def test_concurrent_roles_do_not_create_phantom_gap():
    """A short role nested inside a long one must not read as a gap afterwards.

    Measuring against the running maximum end date rather than the previous
    role's end is what prevents this.
    """
    roles = [
        role("Long", "2023-01", "2025-06"),
        role("Short", "2024-01", "2024-03"),
        role("Next", "2025-07", None),
    ]
    assert [f for f in audit_dates(roles, today=TODAY) if f.kind == "employment_gap"] == []


def test_roles_without_dates_are_skipped_not_crashed():
    assert audit_dates([Role(title="T", org="O", start="", end=None)], today=TODAY) == []
