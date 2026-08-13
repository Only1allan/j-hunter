"""Salary parsing, remote classification, disqualification, and pay ranking.

The stakes here are asymmetric in both directions: a *missed* salary costs one
ranking signal, while a *misparsed* one shows the user a wrong number. And a
missed "US only" restriction wastes an entire application on a job the candidate
cannot legally take, which is the single most expensive failure in this pipeline.
"""

import pytest

from src.contracts import Job, Preferences, RemotePrefs, SalaryPrefs
from src.rank import (
    classify_remote,
    dedupe,
    disqualify,
    job_id,
    normalize_company,
    normalize_title,
    parse_salary,
    process,
    score_pay,
    to_usd,
)


def make(**kw) -> Job:
    base = dict(
        id="x", source="greenhouse", company="Acme", title="Senior Software Engineer",
        location="Remote", description="",
    )
    base.update(kw)
    base["id"] = job_id(base["source"], base["company"], base["title"])
    return Job(**base)


def prefs(**kw) -> Preferences:
    base = dict(
        salary=SalaryPrefs(floor_usd=80_000, require_explicit=False),
        remote=RemotePrefs(
            required=True,
            accept=["worldwide", "global", "remote"],
            reject=["us only", "hybrid", "onsite"],
        ),
        titles=["software engineer", "cloud architect"],
        seniority_min="mid",
        exclude=["intern", "junior", "graduate"],
    )
    base.update(kw)
    return Preferences(**base)


# --- salary parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_min,expected_max",
    [
        ("$120,000 - $150,000 per year", 120_000, 150_000),
        ("Salary: 120k-150k USD", 120_000, 150_000),
        ("€90,000 – €110,000", 90_000, 110_000),
        ("Base pay range: $200,000 to $250,000", 200_000, 250_000),
        ("compensation of $185,000", 185_000, None),
    ],
)
def test_parse_salary_ranges(text, expected_min, expected_max):
    lo, hi, _ = parse_salary(text)
    assert lo == expected_min
    assert hi == expected_max


def test_parse_salary_annualises_hourly():
    lo, hi, _ = parse_salary("$60 - $80 per hour")
    assert lo == 60 * 2080
    assert hi == 80 * 2080


def test_parse_salary_reverses_backwards_range():
    lo, hi, _ = parse_salary("$150,000 - $120,000")
    assert (lo, hi) == (120_000, 150_000)


def test_parse_salary_detects_currency():
    assert parse_salary("GBP 90,000 - 110,000")[2] == "GBP"
    assert parse_salary("€90,000 – €110,000")[2] == "EUR"


def test_parse_salary_ignores_small_unrelated_numbers():
    # "401k" and headcount must not be read as compensation.
    lo, _, _ = parse_salary("Join our team of 250 people. We offer a 401k match.")
    assert lo is None or lo >= 10_000


def test_parse_salary_empty():
    assert parse_salary("") == (None, None, "")


def test_to_usd_converts():
    assert to_usd(100_000, "USD") == 100_000
    assert to_usd(100_000, "EUR") == 108_000
    assert to_usd(None, "USD") is None


# --- remote classification ---------------------------------------------------


def test_worldwide_detected():
    assert classify_remote("Senior Engineer", "Remote - Worldwide", "") == "worldwide"


def test_location_lock_beats_generic_remote():
    """The most important case in this module.

    A posting titled "Remote" whose body says "must reside in the United States"
    is not available to a Nairobi-based candidate. Generic 'remote' must never
    win over an explicit lock.
    """
    scope = classify_remote(
        "Senior Engineer (Remote)", "Remote", "This role is US only."
    )
    assert scope == "country_locked"

    scope = classify_remote(
        "Backend Engineer", "Remote, USA",
        "You must be authorized to work in the US without sponsorship.",
    )
    assert scope == "country_locked"


def test_region_detected():
    assert classify_remote("Engineer", "Remote EMEA", "") == "emea"
    assert classify_remote("Engineer", "Remote - Africa", "") == "africa"


def test_onsite_detected():
    assert classify_remote("Engineer", "Berlin", "This is a hybrid role.") == "onsite"


def test_unclear_when_no_signal():
    assert classify_remote("Engineer", "", "") == "unclear"


# --- disqualification --------------------------------------------------------


def test_country_locked_is_dropped():
    job = make(remote_scope="country_locked")
    assert disqualify(job, prefs()) is not None


def test_onsite_is_dropped():
    assert disqualify(make(remote_scope="onsite"), prefs()) is not None


def test_excluded_title_is_dropped():
    job = make(title="Junior Software Engineer", remote_scope="worldwide")
    assert "excluded title term" in disqualify(job, prefs())


def test_intern_is_dropped():
    job = make(title="Software Engineer Intern", remote_scope="worldwide")
    assert disqualify(job, prefs()) is not None


def test_engineering_title_kept_even_if_wording_differs():
    # "Site Reliability Engineer" isn't in the titles list but is clearly in scope.
    job = make(title="Site Reliability Engineer", remote_scope="worldwide")
    assert disqualify(job, prefs()) is None


def test_unrelated_title_dropped():
    job = make(title="Head of Talent Acquisition", remote_scope="worldwide")
    assert disqualify(job, prefs()) is not None


def test_stated_pay_below_floor_is_dropped():
    job = make(
        remote_scope="worldwide", salary_explicit=True, salary_usd_estimate=40_000
    )
    assert "below floor" in disqualify(job, prefs())


def test_unstated_salary_is_kept_by_default():
    """The core ranking decision: unstated pay must not disqualify.

    Most postings omit compensation; a hard filter would discard the majority of
    the market and make the 100-job target unreachable.
    """
    job = make(remote_scope="worldwide", salary_explicit=False)
    assert disqualify(job, prefs()) is None


def test_require_explicit_drops_unstated():
    job = make(remote_scope="worldwide", salary_explicit=False)
    strict = prefs(salary=SalaryPrefs(floor_usd=80_000, require_explicit=True))
    assert disqualify(job, strict) is not None


# --- pay scoring -------------------------------------------------------------


def test_explicit_high_salary_outscores_unstated():
    rich = make(
        remote_scope="worldwide", salary_explicit=True, salary_usd_estimate=250_000
    )
    vague = make(remote_scope="worldwide", company="Unknown Ltd")
    assert score_pay(rich, prefs())[0] > score_pay(vague, prefs())[0]


def test_seniority_signal_lifts_unstated_job():
    senior = make(title="Principal Software Engineer", remote_scope="worldwide")
    plain = make(title="Software Engineer", remote_scope="worldwide")
    assert score_pay(senior, prefs())[0] > score_pay(plain, prefs())[0]


def test_global_rate_company_lifts_score():
    known = make(company="Stripe", remote_scope="worldwide")
    unknown = make(company="Some Local Shop", remote_scope="worldwide")
    assert score_pay(known, prefs())[0] > score_pay(unknown, prefs())[0]


def test_local_currency_penalised():
    local = make(
        remote_scope="worldwide", description="Salary paid in KES monthly.",
        company="Local Co",
    )
    neutral = make(remote_scope="worldwide", company="Local Co")
    assert score_pay(local, prefs())[0] < score_pay(neutral, prefs())[0]


def test_worldwide_beats_region_locked():
    a = make(remote_scope="worldwide")
    b = make(remote_scope="region_locked")
    assert score_pay(a, prefs())[0] > score_pay(b, prefs())[0]


def test_score_is_bounded_and_explained():
    score, why = score_pay(
        make(remote_scope="worldwide", salary_explicit=True,
             salary_usd_estimate=5_000_000),
        prefs(),
    )
    assert 0 <= score <= 100
    assert why, "pay_rationale must record which signals fired"


# --- dedupe and pipeline -----------------------------------------------------


def test_normalize_company_strips_suffixes():
    assert normalize_company("Acme Inc.") == normalize_company("acme")
    assert normalize_company("Foo Ltd") == normalize_company("Foo")


def test_normalize_title_strips_decoration():
    assert normalize_title("Senior Engineer (Remote)") == normalize_title("senior engineer")
    assert normalize_title("Backend Engineer - EMEA") == normalize_title("backend engineer")


def test_dedupe_prefers_ats_source():
    a = make(source="remotive", description="short")
    b = make(source="greenhouse", description="a much longer full description")
    kept = dedupe([a, b])
    assert len(kept) == 1
    assert kept[0].source == "greenhouse"


def test_dedupe_prefers_longer_description_within_same_source():
    a = make(source="remotive", description="short")
    b = make(source="remotive", description="considerably longer text here")
    kept = dedupe([a, b])
    assert len(kept) == 1 and len(kept[0].description) > len("short")


def test_process_sorts_by_pay_and_caps():
    jobs = [
        make(company=f"Co{i}", title="Senior Software Engineer",
             remote_scope="worldwide", salary_explicit=True,
             salary_usd_estimate=90_000 + i * 10_000)
        for i in range(5)
    ]
    kept, _ = process(jobs, prefs(target_count=3))
    assert len(kept) == 3
    assert kept == sorted(kept, key=lambda j: -j.pay_score)


def test_process_extracts_salary_from_description():
    job = make(remote_scope="worldwide",
               description="The base salary range is $140,000 - $180,000 USD.")
    kept, _ = process([job], prefs())
    assert kept and kept[0].salary_explicit
    assert kept[0].salary_usd_estimate == 180_000


# --- non-engineering roles that contain "engineer" ---------------------------


@pytest.mark.parametrize("title", [
    "Commercial Sales Engineer",
    "Enterprise Sales Engineer - Central",
    "Solutions Engineer",
    "Technical Support Engineer",
    "Field Engineer, Americas",
])
def test_sales_and_support_engineer_titles_are_dropped(title):
    """These pass a naive /engineer/ regex but are not software roles.

    They were genuinely reaching the output queue before the exclusion list
    covered them, which wastes an application each.
    """
    job = make(title=title, remote_scope="worldwide")
    assert disqualify(job, prefs(exclude=[
        "intern", "junior", "sales engineer", "solutions engineer", "sales",
        "support engineer", "field engineer",
    ])) is not None


def test_solutions_architect_still_kept():
    """Guard against over-blocking: this IS a target title on the CV."""
    job = make(title="Senior Solutions Architect", remote_scope="worldwide")
    keep = prefs(
        titles=["software engineer", "solutions architect"],
        exclude=["sales engineer", "solutions engineer", "sales"],
    )
    assert disqualify(job, keep) is None
