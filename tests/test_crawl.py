"""Extraction from crawled job pages.

These fixtures mirror the real markup as verified live: BrighterMonday nests the
posting in an `@graph` and refers to the employer by `@id`, Fuzu inlines a plain
JobPosting. Parsing is tested without touching the network — the crawl itself is
Crawlee's job, the extraction is ours, and only the second part can break subtly.
"""

import pytest

from src.crawl import (
    PLANS,
    BoardCrawler,
    _company,
    _location_text,
    _salary,
    find_job_postings,
    ld_nodes,
)

# Shape verified against https://www.brightermonday.co.ke/listings/... — the
# employer is a separate Organization node referenced by @id, sitting alongside
# the job board's own Organization entries.
BRIGHTERMONDAY_HTML = """
<html><head>
<title>Backend Developer at Simplepay Capital Limited | BrighterMonday</title>
<meta property="og:title" content="Backend Developer in Nairobi" />
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","@id":"https://x/#/schema/Organization/site","name":"BrighterMonday Kenya"},
 {"@type":"Organization","@id":"https://x/#/schema/Organization/parent","name":"Ringier"},
 {"@type":"JobPosting","title":"Backend Developer","datePosted":"2026-07-02T00:00:00Z",
  "employmentType":"FULL_TIME",
  "description":"<p>Build APIs.</p><li>Python</li>",
  "hiringOrganization":{"@id":"https://x/#/schema/Organization/agency-118"},
  "jobLocation":{"address":{"addressRegion":"Nairobi","addressCountry":"KE"}}},
 {"@type":"Organization","@id":"https://x/#/schema/Organization/agency-118","name":"Simplepay Capital Limited"}
]}
</script></head><body></body></html>
"""

FUZU_HTML = """
<html><head>
<title>Assistant IT Manager at Optica Limited | Fuzu</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Assistant IT Manager","datePosted":"2026-06-11",
 "description":"<p>Run the helpdesk.</p>",
 "hiringOrganization":{"@type":"Organization","name":"Optica Limited"},
 "jobLocation":{"address":{"addressLocality":"Nairobi","addressCountry":"KE"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"KES",
   "value":{"@type":"QuantitativeValue","minValue":55000,"maxValue":75000,"unitText":"MONTH"}}}
</script></head><body></body></html>
"""


def crawler(name: str = "brightermonday") -> BoardCrawler:
    return BoardCrawler(PLANS[name])


# --- JSON-LD discovery -------------------------------------------------------


def test_finds_posting_nested_in_a_graph():
    postings = find_job_postings(BRIGHTERMONDAY_HTML)
    assert len(postings) == 1
    assert postings[0]["title"] == "Backend Developer"


def test_finds_top_level_posting():
    assert find_job_postings(FUZU_HTML)[0]["title"] == "Assistant IT Manager"


def test_page_without_structured_data_yields_nothing():
    assert find_job_postings("<html><body>No jobs here</body></html>") == []
    assert crawler().parse_page("https://x/listings/a", "<html></html>") is None


def test_malformed_json_ld_does_not_raise():
    html = '<script type="application/ld+json">{not json,,}</script>'
    assert ld_nodes(html) == []


# --- employer resolution -----------------------------------------------------


def test_employer_resolved_through_an_id_reference():
    """The employer is behind an @id and three unrelated Organizations share the
    graph. Taking the first Organization would credit the job board itself."""
    posting = find_job_postings(BRIGHTERMONDAY_HTML)[0]
    assert _company(posting, BRIGHTERMONDAY_HTML, "fallback") == "Simplepay Capital Limited"


def test_inlined_employer_is_used_directly():
    posting = find_job_postings(FUZU_HTML)[0]
    assert _company(posting, FUZU_HTML, "fallback") == "Optica Limited"


def test_employer_falls_back_to_page_title():
    posting = {"title": "Backend Developer"}
    html = "<title>Backend Developer at Acme Ltd | BrighterMonday</title>"
    assert _company(posting, html, "fallback") == "Acme Ltd"


def test_employer_falls_back_to_placeholder_when_unknowable():
    assert _company({}, "<html></html>", "BrighterMonday listing") == "BrighterMonday listing"


# --- location and salary -----------------------------------------------------


def test_location_is_flattened_without_repeats():
    posting = find_job_postings(FUZU_HTML)[0]
    assert _location_text(posting) == "Nairobi, KE"


def test_telecommute_flag_marks_remote():
    posting = {"jobLocationType": "TELECOMMUTE",
               "jobLocation": {"address": {"addressCountry": "KE"}}}
    assert "Remote" in _location_text(posting)


def test_monthly_salary_is_annualised():
    """Kenyan postings quote monthly. 55,000 KES/month is a solid local salary;
    stored as an annual figure it would rank below every intern role abroad."""
    lo, hi, currency = _salary(find_job_postings(FUZU_HTML)[0])
    assert (lo, hi, currency) == (55_000 * 12, 75_000 * 12, "KES")


def test_missing_salary_is_none_not_zero():
    assert _salary(find_job_postings(BRIGHTERMONDAY_HTML)[0]) == (None, None, "")


# --- whole-page parsing ------------------------------------------------------


def test_parse_page_builds_a_complete_job():
    job = crawler().parse_page(
        "https://www.brightermonday.co.ke/listings/backend-developer-z8v44d",
        BRIGHTERMONDAY_HTML,
    )
    assert job is not None
    assert job.title == "Backend Developer"
    assert job.company == "Simplepay Capital Limited"
    assert job.source == "brightermonday"
    assert job.posted_at == "2026-07-02"
    assert "Build APIs." in job.description
    assert "<p>" not in job.description, "HTML must be stripped from the description"
    assert job.apply_url.endswith("backend-developer-z8v44d")


def test_parse_page_carries_salary_through():
    job = crawler("fuzu").parse_page("https://www.fuzu.com/kenya/jobs/x", FUZU_HTML)
    assert job.salary_explicit
    assert job.salary_currency == "KES"
    assert job.salary_usd_estimate and job.salary_usd_estimate > 0


# --- sitemap walking ---------------------------------------------------------


@pytest.mark.parametrize("name,url,is_job", [
    ("brightermonday", "https://www.brightermonday.co.ke/listings/backend-dev-z8v44d", True),
    # robots.txt disallows /job/ on BrighterMonday; such URLs are not job pages
    # for this plan and Crawlee would refuse them anyway.
    ("brightermonday", "https://www.brightermonday.co.ke/job/12345", False),
    ("fuzu", "https://www.fuzu.com/kenya/jobs/assistant-it-manager-optica", True),
    ("fuzu", "https://www.fuzu.com/kenya/sitemap-computers-listings.xml.gz", False),
])
def test_job_url_patterns(name, url, is_job):
    assert bool(PLANS[name].job_url_re.search(url)) is is_job


def test_every_plan_declares_reachable_sitemaps():
    for plan in PLANS.values():
        assert plan.sitemaps, f"{plan.name} has no sitemap entry point"
        assert all(u.startswith("https://") for u in plan.sitemaps)
