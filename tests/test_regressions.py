"""Regression tests for defects that were reproduced in the running pipeline.

Every test here corresponds to a bug that was observably corrupting output, not to
a hypothetical. The docstrings record the concrete input that produced the wrong
answer, because that is the part that is expensive to rediscover.
"""

import csv
from collections import Counter

import pytest

from src.contracts import ApplicationPackage, Job, MatchScore, Preferences, RemotePrefs
from src.rank import (
    classify_remote,
    dedupe,
    disqualify,
    job_id,
    parse_salary,
    score_pay,
    to_usd,
)


def make(**kw) -> Job:
    base = dict(
        source="greenhouse", company="Acme", title="Senior Software Engineer",
        location="Remote", description="",
    )
    base.update(kw)
    base["id"] = job_id(base["source"], base["company"], base["title"],
                        base.get("location", ""))
    return Job(**base)


def prefs(**kw) -> Preferences:
    base = dict(
        titles=["software engineer"],
        exclude=["intern", "junior"],
        remote=RemotePrefs(
            required=True,
            accept=["worldwide", "remote"],
            reject=["us only", "hybrid", "onsite", "work authorization required"],
        ),
    )
    base.update(kw)
    return Preferences(**base)


# --- parse_salary: numbers that are not money --------------------------------


@pytest.mark.parametrize("text", [
    # Was parsed as $6,240-$10,400 and then disqualified the job as "below floor".
    "We are looking for someone with 3-5 years of experience building systems.",
    "Requires 5-8 years of experience in distributed systems.",
    # Was parsed as a $100,000-$500,000 range and sorted to the top of the queue.
    "Serving 100,000 - 500,000 users daily.",
    "Trusted by 50,000 - 90,000 customers worldwide.",
    "Our platform processes 10-15 million events per second.",
    # Company financials carry a currency symbol and passed every other check.
    "We raised $20,000,000 in Series B funding.",
    "Our ARR passed $50,000,000 last year.",
    "Backed by investors at a $1,200,000,000 valuation.",
    "We manage $30,000,000 in assets under management.",
    # Perks are not salary.
    "Enjoy a $5,000 signing bonus and 401k matching.",
    "There is a $2,000 annual learning budget.",
])
def test_non_salary_numbers_are_not_parsed_as_pay(text):
    assert parse_salary(text)[0] is None, f"misparsed as salary: {text!r}"


@pytest.mark.parametrize("text,expected_min", [
    ("Salary: $120,000 - $150,000 per year", 120_000),
    ("$120,000 - $150,000 per year, plus equity and a signing bonus", 120_000),
    ("Base pay range: $200,000 to $250,000", 200_000),
    ("compensation of $185,000", 185_000),
    ("The base salary range is $140,000 - $180,000 USD.", 140_000),
])
def test_real_salaries_still_parse(text, expected_min):
    """Guard against over-correction: the anti-false-positive rules must not
    start rejecting genuine compensation statements."""
    assert parse_salary(text)[0] == expected_min


@pytest.mark.parametrize("text,expected", [
    # Kenyan and wider African postings quote monthly. Reading these as annual
    # figures understates a good local job by 12x and buries it in the ranking.
    ("Gross salary: KES 400,000 - 550,000 per month", 4_800_000),
    ("Salary NGN 1,500,000 monthly", 18_000_000),
    ("Compensation: ZAR 90,000 per month", 1_080_000),
])
def test_monthly_pay_is_annualised(text, expected):
    assert parse_salary(text)[0] == expected


def test_currency_code_before_the_number_is_detected():
    """"KES 400,000" is the African convention; only "400,000 KES" used to work."""
    assert parse_salary("Salary NGN 1,500,000 monthly")[2] == "NGN"
    assert parse_salary("Compensation: ZAR 90,000 per month")[2] == "ZAR"


@pytest.mark.parametrize("text", [
    "You should try 3 different approaches.",
    "Our police cop program covers 5 cities.",
])
def test_currency_codes_that_are_english_words_do_not_match_prose(text):
    """TRY, COP, RON and MAD are currency codes and ordinary words. Matching them
    case-insensitively turns narrative prose into compensation data."""
    assert parse_salary(text)[0] is None
    assert parse_salary(text)[2] == "", "no currency should be reported either"


# --- hourly inference: found in a live scrape --------------------------------


def test_typod_range_does_not_become_an_hourly_rate():
    """Adyen's live posting reads "$300,00 - $367,000" — a missing zero. The
    thousands-separator regex could only read "$300", which was then assumed to
    be an hourly rate and multiplied to $624,000, putting a director role at the
    very top of the pay-ranked queue. The correct reading is the range maximum."""
    lo, _, _ = parse_salary(
        "The annual base salary range for this role is $300,00 - $367,000 in SF"
    )
    assert lo == 367_000


@pytest.mark.parametrize("text", [
    "Plans start at $200 for the pro tier.",
    "A $300 hardware stipend is provided.",
    "Our platform serves 400 enterprise accounts.",
])
def test_lone_small_figures_are_never_treated_as_hourly(text):
    """A bare sub-500 number is far more often a price, a typo or a count than an
    unlabelled hourly rate; multiplying by 2080 invents a six-figure salary."""
    assert parse_salary(text)[0] is None


@pytest.mark.parametrize("text,expected", [
    ("generally $90-$150+/hr, with vetted clients", 90 * 2080),
    ("$60 - $80 per hour", 60 * 2080),
    ("Contract rate: $75-$95 an hour", 75 * 2080),
])
def test_explicitly_hourly_rates_still_annualise(text, expected):
    """Guard the other direction: contractor marketplaces really do quote hourly,
    and those postings are legitimately high-paying."""
    assert parse_salary(text)[0] == expected


# --- to_usd: unknown currencies ----------------------------------------------


def test_unknown_currency_converts_to_none_not_itself():
    """8,000,000 JPY was reported as "$8,000,000" and sorted to the top of the
    queue. An unknown rate must produce "unknown", never a 1:1 passthrough."""
    assert to_usd(100_000, "XYZ") is None
    assert to_usd(8_000_000, "JPY") == 52_000


def test_unconvertible_pay_falls_back_to_proxy_scoring():
    job = make(remote_scope="worldwide", salary_explicit=True,
               salary_currency="XYZ", salary_usd_estimate=None)
    score, why = score_pay(job, prefs())
    assert 0 <= score <= 100
    assert any("no FX rate" in reason for reason in why)


# --- classify_remote: precedence ---------------------------------------------


@pytest.mark.parametrize("title,location,description,expected", [
    # "our global team" is marketing copy. It was promoting on-site roles to
    # `worldwide`, the single best-paying scope, and adding +10 to their pay score.
    ("Senior Engineer", "New York, NY (On-site)",
     "Join our global team of 500 engineers.", "onsite"),
    ("Backend Engineer", "Berlin",
     "This is a hybrid role, 3 days in office. We are a global leader.", "onsite"),
    ("Engineer", "Cape Town, South Africa",
     "Onsite in our Cape Town office.", "onsite"),
    # Genuine worldwide claims must still win.
    ("Engineer", "Remote - Worldwide", "", "worldwide"),
    ("Engineer", "Remote", "You can work from anywhere.", "worldwide"),
    # A negated on-site mention is not an on-site policy.
    ("Engineer", "Remote", "We do not offer hybrid or onsite arrangements.", "global"),
    # Location locks outrank everything.
    ("Engineer (Remote)", "Remote", "This role is US only.", "country_locked"),
])
def test_remote_scope_precedence(title, location, description, expected):
    assert classify_remote(title, location, description) == expected


def test_south_africa_is_not_a_bare_africa_region_match_over_onsite():
    """Substring matching made "South Africa" register as an africa region-lock,
    which outranked the explicit on-site signal in the same posting."""
    assert classify_remote("Engineer", "Cape Town, South Africa",
                           "Onsite in our Cape Town office.") == "onsite"


# --- job_id: distinct postings must stay distinct ----------------------------


def test_same_title_different_city_are_different_jobs():
    """Stripe posts "Software Engineer, Payments" in several cities. Hashing only
    company+title collapsed them, so dedupe silently discarded all but one — each
    has its own application form."""
    dublin = job_id("greenhouse", "Stripe", "Software Engineer, Payments", "Dublin, Ireland")
    seattle = job_id("greenhouse", "Stripe", "Software Engineer, Payments", "Seattle, WA")
    assert dublin != seattle


def test_remote_phrasing_variants_still_collapse():
    """The location must not be so literal that "Dublin" and "Remote - Dublin"
    become two jobs. Remote-ness is erased; the city is kept."""
    a = job_id("greenhouse", "Stripe", "Software Engineer", "Dublin, Ireland")
    b = job_id("greenhouse", "Stripe", "Software Engineer", "Remote - Dublin, Ireland")
    assert a == b


def test_dedupe_keeps_both_city_variants():
    jobs = [
        make(company="Stripe", title="Software Engineer, Payments",
             location="Dublin, Ireland"),
        make(company="Stripe", title="Software Engineer, Payments",
             location="Seattle, WA"),
    ]
    assert len(dedupe(jobs)) == 2


# --- disqualify: boilerplate must not drop good jobs -------------------------


def test_negated_and_eeo_boilerplate_does_not_disqualify():
    """A fully-remote worldwide job was dropped because its equal-opportunity
    boilerplate contained "work authorization required" and its remote policy
    said "we do not offer hybrid"."""
    job = make(
        location="Remote - Worldwide", remote_scope="worldwide",
        description=(
            "Fully remote worldwide. We do not offer hybrid or onsite arrangements. "
            "Acme is an equal opportunity employer; work authorization required "
            "regardless of race, veteran status or protected class."
        ),
    )
    assert disqualify(job, prefs()) is None


def test_genuine_hybrid_requirement_is_still_dropped():
    """Guard against over-correction in the opposite direction."""
    job = make(
        location="Remote", remote_scope="global",
        description="This is a hybrid position requiring 3 days per week in Austin.",
    )
    assert disqualify(job, prefs()) is not None


def test_restriction_in_the_location_field_is_taken_at_face_value():
    job = make(location="Remote (US only)", remote_scope="global")
    assert disqualify(job, prefs()) is not None


# --- remote.accept was dead config -------------------------------------------


def test_accepted_arrangement_is_a_positive_signal():
    """`Preferences.remote.accept` was parsed from preferences.yaml and then never
    read by any code path."""
    # Same remote_scope on both, so the only thing that can differ is whether the
    # location matches an arrangement the candidate said they would accept.
    accepted = make(location="Remote - EMEA", remote_scope="emea")
    ignored = make(location="Nairobi, Kenya", remote_scope="emea")
    assert score_pay(accepted, prefs())[0] > score_pay(ignored, prefs())[0]
    assert any("accepted arrangement" in r for r in score_pay(accepted, prefs())[1])


# --- manifest merge: the data-loss bug ---------------------------------------


def _package(slug: str, score: int = 90) -> ApplicationPackage:
    job = make(company=slug, title="Senior Software Engineer")
    return ApplicationPackage(
        job=job,
        match=MatchScore(score=score, persona="p", rationale="r"),
        dir=f"output/{slug}",
        resume_pages=1,
        status="generated",
    )


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    from src import config

    monkeypatch.setattr(config, "OUTBOUND", tmp_path)
    monkeypatch.setattr(config, "MANIFEST_CSV", tmp_path / "manifest.csv")
    return tmp_path / "manifest.csv"


def test_regeneration_preserves_hand_entered_application_history(manifest):
    """`generate` rewrote manifest.csv from scratch on every run, erasing the
    applied_on/response columns the user fills in by hand. Losing the record of
    what was already sent causes duplicate applications to the same employer."""
    from src import generate as gen

    gen.write_manifest([_package("alpha")])

    rows = list(csv.DictReader(manifest.open()))
    rows[0]["applied_on"] = "2026-08-01"
    rows[0]["response"] = "phone screen booked"
    with manifest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=gen.MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    gen.write_manifest([_package("alpha"), _package("beta", score=80)])

    after = {r["package_dir"]: r for r in csv.DictReader(manifest.open())}
    assert after["output/alpha"]["applied_on"] == "2026-08-01"
    assert after["output/alpha"]["response"] == "phone screen booked"
    assert "output/beta" in after


def test_rows_from_earlier_runs_are_not_dropped(manifest):
    """A package absent from the current batch must survive — it is still a job
    you applied to."""
    from src import generate as gen

    gen.write_manifest([_package("alpha")])
    gen.write_manifest([_package("beta")])

    after = {r["package_dir"] for r in csv.DictReader(manifest.open())}
    assert after == {"output/alpha", "output/beta"}


def test_manifest_survives_commas_in_job_titles(manifest):
    """`status` counted applications by splitting each line on ",", which shifted
    every column for the very common "Software Engineer, Payments" style title."""
    from src import generate as gen

    pkg = _package("alpha")
    pkg.job.title = "Software Engineer, Payments, Dublin"
    gen.write_manifest([pkg])

    rows = gen.read_manifest()
    assert rows["output/alpha"]["role"] == "Software Engineer, Payments, Dublin"
    assert rows["output/alpha"]["applied_on"] == ""


# --- jobapp run: typer default leakage ---------------------------------------


def test_run_passes_real_defaults_not_typer_sentinels(monkeypatch):
    """`run` called the typer command functions directly, so every parameter it
    omitted arrived as an `OptionInfo` sentinel. `OptionInfo` is truthy, so
    `pool or <default>` kept it, and `all_jobs[:pool_size]` raised TypeError —
    `jobapp run` could not complete at all."""
    from src import cli

    seen = {}

    monkeypatch.setattr(cli, "_scrape", lambda **kw: seen.update(scrape=kw))
    monkeypatch.setattr(cli, "_generate", lambda **kw: seen.update(generate=kw))

    cli.run(limit=3)

    assert seen["generate"] == {"limit": 3}
    for value in seen["generate"].values():
        assert isinstance(value, int), f"typer sentinel leaked: {value!r}"


def test_generate_helper_defaults_are_plain_python_values():
    import inspect

    from src import cli, config

    params = inspect.signature(cli._generate).parameters
    assert params["limit"].default == 10
    assert params["pool"].default == 0
    assert params["threshold"].default == config.MATCH_THRESHOLD
    assert params["persona"].default == "senior-tech-recruiter"


# --- sources: empty board vs unreachable board -------------------------------


def test_empty_board_is_reported_separately_from_a_broken_one(capsys):
    """Both were reported as "no jobs from N board(s)", so a company that simply
    is not hiring looked identical to a token that needs updating."""
    from src.sources import BoardUnavailable, collect

    jobs = collect(
        "greenhouse",
        ["healthy", "notrhiring", "deadtoken"],
        [[make()], [], BoardUnavailable("deadtoken: 404")],
    )
    out = capsys.readouterr().out
    assert len(jobs) == 1
    assert "deadtoken" in out and "unreachable" in out
    assert "notrhiring" in out and "hiring nothing" in out


# --- web: path traversal on package slugs ------------------------------------


@pytest.mark.parametrize("slug", ["../../etc", "..", "foo/../../bar", "", "a b"])
def test_package_slug_rejects_traversal(slug):
    from fastapi import HTTPException

    from src.web import package_dir

    with pytest.raises(HTTPException):
        package_dir(slug)


# --- skill-weighted ranking --------------------------------------------------

from src.contracts import SalaryPrefs, SkillPrefs  # noqa: E402
from src.rank import (  # noqa: E402
    combine_scores, detect_tech, process, score_skills,
)

CANDIDATE_SKILLS = [
    "Python", "TypeScript", "JavaScript", "Java", "C", "Bash", "SQL", "React",
    "Next.js", "Node.js", "FastAPI", "GraphQL", "REST", "AWS", "Azure",
    "Terraform", "LangChain", "Bedrock", "Tailwind",
]


def fit_prefs(**kw) -> Preferences:
    base = dict(
        salary=SalaryPrefs(floor_usd=25_000, ceiling_usd=400_000),
        skills=SkillPrefs(have=CANDIDATE_SKILLS, learning=["Docker", "Kubernetes"]),
        titles=["software engineer"],
        exclude=["intern"],
    )
    base.update(kw)
    return Preferences(**base)


def paid(usd: int, title: str, description: str = "", **kw) -> Job:
    """A job with a stated salary. Identity fields must be passed here, not set
    afterwards: `make` hashes the id from company+title+location at construction,
    so mutating them later leaves every job sharing one id and `dedupe` collapses
    the lot into a single row."""
    fields = dict(title=title, description=description, remote_scope="worldwide",
                  salary_explicit=True, salary_usd_estimate=usd,
                  salary_currency="USD")
    fields.update(kw)
    return make(**fields)


def fit_of(job: Job, prefs: Preferences) -> int:
    return combine_scores(score_pay(job, prefs)[0], score_skills(job, prefs)[0])


def test_pay_curve_still_discriminates_at_a_low_floor():
    """With a $25k floor the old ratio formula gave 100 to everything above
    $50k, so ordering by "highest paying" stopped ordering anything."""
    prefs = fit_prefs()
    scores = [score_pay(paid(usd, "Senior Software Engineer"), prefs)[0]
              for usd in (30_000, 50_000, 100_000, 200_000, 400_000)]
    assert scores == sorted(scores), "pay score must increase with pay"
    assert len(set(scores)) == len(scores), "each pay band must be distinguishable"


def test_better_paid_job_in_an_unknown_language_ranks_below_a_lesser_paid_match():
    """The explicit requirement: $70k requiring Rust is a worse lead than $50k
    requiring TypeScript. The first is a rejection; the second is an interview."""
    prefs = fit_prefs()
    rust = paid(70_000, "Senior Rust Engineer",
                "We require strong experience in Rust and systems programming.")
    typescript = paid(50_000, "Senior Software Engineer",
                      "Strong TypeScript and React required. Node.js on AWS.")
    assert score_pay(rust, prefs)[0] > score_pay(typescript, prefs)[0], (
        "precondition: the Rust job really does pay more"
    )
    assert fit_of(rust, prefs) < fit_of(typescript, prefs)


def test_a_high_salary_cannot_buy_past_a_stack_mismatch():
    """Multiplicative, not additive: with an additive blend a big enough number
    always outweighs a language the candidate cannot write."""
    prefs = fit_prefs()
    rich_mismatch = paid(300_000, "Principal Go Engineer",
                         "Deep expertise in Golang and distributed systems required.")
    modest_match = paid(90_000, "Senior Software Engineer",
                        "Python and TypeScript, FastAPI, AWS, Terraform.")
    assert fit_of(rich_mismatch, prefs) < fit_of(modest_match, prefs)


def test_a_well_paid_job_in_the_right_stack_still_wins_overall():
    """Guard the other direction — the point is to rank pay by fit, not to
    penalise pay."""
    prefs = fit_prefs()
    great = paid(180_000, "Senior Platform Engineer",
                 "Python, AWS, Terraform, Kubernetes. TypeScript a plus.")
    modest = paid(60_000, "Software Engineer", "Python and React.")
    assert fit_of(great, prefs) > fit_of(modest, prefs)


def test_missing_framework_is_not_penalised_like_a_missing_language():
    """Django is a weekend; Rust is a career. They must not cost the same."""
    prefs = fit_prefs()
    framework_gap = paid(80_000, "Senior Software Engineer",
                         "Python backend using Django. AWS infrastructure.")
    language_gap = paid(80_000, "Senior Software Engineer",
                        "Backend written in Rust. Requires strong Rust experience.")
    assert score_skills(framework_gap, prefs)[0] > score_skills(language_gap, prefs)[0]


def test_language_named_in_the_title_is_penalised_harder_than_one_in_passing():
    prefs = fit_prefs()
    in_title = paid(80_000, "Senior Golang Engineer", "Build services in Go.")
    in_passing = paid(80_000, "Senior Software Engineer",
                      "Python and TypeScript. Some services are written in Go.")
    assert score_skills(in_title, prefs)[0] < score_skills(in_passing, prefs)[0]


def test_posting_naming_no_technology_scores_neutral():
    prefs = fit_prefs()
    score, why = score_skills(
        paid(80_000, "Senior Software Engineer", "Join a great team."), prefs
    )
    assert 40 <= score <= 70
    assert any("no specific technology" in r for r in why)


def test_skill_rationale_explains_the_score():
    prefs = fit_prefs()
    _, why = score_skills(
        paid(80_000, "Senior Rust Engineer", "Rust required."), prefs
    )
    assert any("rust" in r.lower() for r in why), "must name what it penalised"


def test_detect_tech_separates_languages_from_tooling():
    job = paid(0, "Engineer", "Python and React with Terraform on AWS.")
    core, support = detect_tech(job)
    assert "python" in core
    assert {"react", "terraform", "aws"} <= support
    assert "react" not in core, "a framework is not a core language"


def test_process_ranks_by_fit_not_pay_alone():
    prefs = fit_prefs(target_count=10)
    jobs = [
        paid(120_000, "Senior Rust Engineer", "Requires expert Rust."),
        paid(70_000, "Senior Software Engineer", "TypeScript, React, Node.js, AWS."),
    ]
    kept, _ = process(jobs, prefs)
    assert kept[0].title.startswith("Senior Software Engineer"), (
        "the fitting job must come first despite the lower salary"
    )
    assert all(j.fit_score > 0 for j in kept)
    assert kept[0].skill_rationale, "every job must explain its fit score"


def test_fit_score_is_bounded_to_100():
    """A perfect stack match adds a premium on top of a top-of-scale salary, so
    clamping only the pay term let the result reach 111/100 — which is not a
    score anyone can reason about, and broke sorting against other 100s."""
    assert combine_scores(100, 100) == 100
    assert combine_scores(0, 0) == 0
    for pay in range(0, 101, 10):
        for skill in range(0, 101, 10):
            assert 0 <= combine_scores(pay, skill) <= 100


# --- segment quotas ----------------------------------------------------------

from src.contracts import Segment  # noqa: E402

KENYA = Segment(
    name="kenya", quota=2,
    match_locations=["kenya", "nairobi"], match_sources=["fuzu", "brightermonday"],
    salary_floor_usd=6_000, allow_onsite=True,
)
GLOBAL = Segment(name="global", quota=3)


def seg_prefs(**kw) -> Preferences:
    base = dict(
        salary=SalaryPrefs(floor_usd=25_000, ceiling_usd=400_000),
        skills=SkillPrefs(have=CANDIDATE_SKILLS),
        titles=["software engineer"], exclude=["intern"],
        segments=[KENYA, GLOBAL], target_count=5,
    )
    base.update(kw)
    return Preferences(**base)


def test_onsite_job_at_home_is_not_dropped_as_not_remote():
    """`remote.required` exists to filter out roles needing a move. An on-site
    Nairobi role needs no visa and no relocation — the candidate lives there —
    so applying that rule to it discarded the entire local on-site market."""
    prefs = seg_prefs()
    job = make(location="Nairobi, Kenya", remote_scope="onsite",
               source="brightermonday")
    segment = prefs.segment_for(location=job.location, source=job.source)
    assert segment.name == "kenya"
    assert disqualify(job, prefs, segment) is None


def test_onsite_job_abroad_is_still_dropped():
    prefs = seg_prefs()
    job = make(location="New York, NY", remote_scope="onsite")
    segment = prefs.segment_for(location=job.location, source=job.source)
    assert segment.name == "global"
    assert disqualify(job, prefs, segment) is not None


def test_local_salary_floor_applies_in_the_local_segment():
    """Measuring Kenyan pay against a global USD floor rejects the whole local
    market before it is ever ranked."""
    prefs = seg_prefs()
    local = make(location="Nairobi, Kenya", source="fuzu", remote_scope="africa",
                 salary_explicit=True, salary_usd_estimate=9_000)
    abroad = make(location="Remote", remote_scope="worldwide",
                  salary_explicit=True, salary_usd_estimate=9_000)
    assert disqualify(local, prefs, prefs.segment_for(
        location=local.location, source=local.source)) is None
    assert disqualify(abroad, prefs, prefs.segment_for(
        location=abroad.location, source=abroad.source)) is not None


def test_catch_all_segment_claims_everything_unmatched():
    prefs = seg_prefs()
    assert prefs.segment_for(location="Berlin, Germany", source="greenhouse").name == "global"
    assert prefs.segment_for(location="Nairobi", source="greenhouse").name == "kenya"


def test_segment_matching_is_on_word_boundaries():
    """Substring matching would put a job in "Kenyatta Avenue, Lagos" or any
    company called "Nairobi Capital" into the local segment."""
    assert not KENYA.matches(location="Kenyanthropus Research, Ethiopia", source="x")
    assert KENYA.matches(location="Nairobi, Kenya", source="x")


def test_quotas_reserve_slots_for_the_local_market():
    """The point of the whole feature: a $300k US remote role outranks every job
    in Nairobi, so without a quota the local market disappears completely."""
    prefs = seg_prefs()
    # Distinct companies: identical company+title+location dedupes to one job.
    rich_global = [
        paid(300_000 - i * 1000, "Senior Software Engineer",
             "Python, TypeScript, AWS.", company=f"Global{i}",
             location="Remote - Worldwide", source="greenhouse")
        for i in range(10)
    ]
    local = [
        paid(20_000 - i * 100, "Senior Software Engineer", "Python and React.",
             company=f"Kenyan{i}", location="Nairobi, Kenya", source="fuzu",
             remote_scope="africa")
        for i in range(5)
    ]

    kept, _ = process(rich_global + local, prefs)
    counts = Counter(j.segment for j in kept)
    assert counts["kenya"] == 2, "the local quota must be honoured"
    assert counts["global"] == 3


def test_unfilled_quota_is_handed_back_rather_than_shrinking_the_queue():
    """If the local market supplies nothing, the queue should still be full."""
    prefs = seg_prefs()
    jobs = [
        paid(200_000 - i * 1000, "Senior Software Engineer", "Python, TypeScript.",
             company=f"Global{i}", location="Remote - Worldwide", source="greenhouse")
        for i in range(10)
    ]
    kept, _ = process(jobs, prefs)
    assert len(kept) == 5, "unfilled local slots must go back to the global pool"


def test_no_segments_configured_keeps_the_original_behaviour():
    prefs = seg_prefs(segments=[], target_count=3)
    jobs = [
        paid(100_000 + i * 1000, "Senior Software Engineer", "Python.",
             company=f"Co{i}")
        for i in range(6)
    ]
    kept, _ = process(jobs, prefs)
    assert len(kept) == 3
    assert kept == sorted(kept, key=lambda j: -j.fit_score)


# --- local currency conventions ----------------------------------------------

from src.rank import assume_monthly_if_implausible, canonical_currency  # noqa: E402


@pytest.mark.parametrize("written,iso", [
    ("KSHS", "KES"), ("KSH", "KES"), ("Ksh", "KES"), ("shs", "KES"),
    ("NAIRA", "NGN"), ("RAND", "ZAR"), ("USH", "UGX"), ("CEDI", "GHS"),
    ("USD", "USD"),
])
def test_currency_aliases_resolve_to_iso_codes(written, iso):
    """Kenyan postings write "KSHS 80,000", not "KES 80,000". An unrecognised
    code silently fell back to USD, turning a KSh 80,000 monthly salary into
    "$120,000" and lifting a manufacturing job into the shortlist."""
    assert canonical_currency(written) == iso


def test_kshs_salary_is_read_as_kenyan_shillings():
    lo, hi, currency = parse_salary(
        "JOB TITLE MECHANICAL ENGINEER. SALARY KSHS 80,000 - 120,000"
    )
    assert currency == "KES"
    assert lo == 80_000, "the figure itself must not be inflated"


def test_local_currency_without_a_period_is_read_as_monthly():
    """"SALARY KSHS 80,000" has no period because monthly is the local default.
    Read as annual it is about $600/year — not a salary, and low enough to be
    dropped as below floor, which quietly deletes the local market."""
    lo, hi = assume_monthly_if_implausible(80_000, 120_000, "KES")
    assert (lo, hi) == (80_000 * 12, 120_000 * 12)
    assert 5_000 < to_usd(hi, "KES") < 20_000


def test_an_already_annual_local_salary_is_left_alone():
    """Guard against double-counting: a figure that already annualises to a
    plausible amount must not be multiplied again."""
    assert assume_monthly_if_implausible(4_800_000, None, "KES") == (4_800_000, None)


def test_usd_salaries_are_never_reinterpreted_as_monthly():
    assert assume_monthly_if_implausible(140_000, 180_000, "USD") == (140_000, 180_000)
    # Even a small USD figure — the convention is local-currency specific.
    assert assume_monthly_if_implausible(2_000, None, "USD") == (2_000, None)


@pytest.mark.parametrize("title", [
    "MECHANICAL ENGINEER",
    "Civil Engineer - Roads",
    "Production Manager/Engineer-Tissues",
    "Electrical Engineer",
    "Quantity Surveyor",
])
def test_non_software_engineering_disciplines_are_excluded(title):
    """The soft title check keeps anything matching /engineer/ so real titles
    like "Site Reliability Engineer" survive. On Kenyan boards that let
    manufacturing and construction roles straight into the shortlist."""
    from src.rank import disqualify as dq

    job = make(title=title, remote_scope="africa", location="Nairobi, Kenya")
    non_software = prefs(exclude=[
        "mechanical engineer", "civil engineer", "electrical engineer",
        "production manager", "quantity surveyor", "production engineer",
    ])
    assert dq(job, non_software) is not None, f"{title!r} should be excluded"


@pytest.mark.parametrize("title", [
    "Site Reliability Engineer",
    "Senior Software Engineer",
    "Platform Engineer",
    "Solutions Architect",
])
def test_real_software_titles_survive_the_discipline_exclusions(title):
    from src.rank import disqualify as dq

    job = make(title=title, remote_scope="worldwide")
    keep = prefs(exclude=[
        "mechanical engineer", "civil engineer", "electrical engineer",
        "production manager", "quantity surveyor",
    ])
    assert dq(job, keep) is None, f"{title!r} must not be excluded"
