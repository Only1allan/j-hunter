"""CV parsing, retrieval, and the render path.

The render tests are the important ones: they pin the guarantee that tailoring can
never alter an employer name, a job title, or a date, because those are copied from
the profile rather than taken from model output.
"""

import pytest
from bs4 import BeautifulSoup

from src import config
from src.contracts import Contact, Profile, Role, SkillGroup, TailoredResume
from src.extract import parse_cv, slugify
from src.generate import render_resume
from src.retrieve import Index, tokenize

TEMPLATE = """<!DOCTYPE html><html><head><style>body{font-size:9.4pt}</style></head>
<body>
<h1>Allan Kariuki</h1>
<div class="title">Full Stack Engineer</div>
<div class="contact">a@b.com | Nairobi</div>
<h2>Professional Summary</h2>
<p class="summary">Original summary.</p>
<h2>Key Highlights</h2>
<ul class="highlights"><li>Old one</li><li>Old two</li></ul>
<h2>Technical Skills</h2>
<div class="skills-row"><div class="skills-label">Languages</div>
<div class="skills-value">Python, Go</div></div>
<div class="skills-row"><div class="skills-label">Cloud</div>
<div class="skills-value">AWS</div></div>
<h2>Professional Experience</h2>
<h3>Software Engineer</h3>
<div class="company-line"><span>Dakota Law Firm · Remote, USA</span><span>Jan 2025 – Jan 2026</span></div>
<ul><li>Original bullet</li></ul>
<h2>Selected Projects</h2>
<p class="summary"><b>Old</b> — thing</p>
</body></html>"""


def sample_profile() -> Profile:
    return Profile(
        contact=Contact(name="Allan Kariuki", email="a@b.com", location="Nairobi"),
        headline="Full Stack Engineer",
        summary="Original summary.",
        skills=[
            SkillGroup(label="Languages", items=["Python", "Go"]),
            SkillGroup(label="Cloud", items=["AWS"]),
        ],
        roles=[
            Role(title="Software Engineer", org="Dakota Law Firm",
                 start="2025-01", end="2026-01", bullets=["Original bullet"])
        ],
    )


# --- rendering: the anti-fabrication guarantees -------------------------------


def test_render_replaces_summary_and_highlights():
    tailored = TailoredResume(
        headline="Cloud Solutions Architect",
        summary="Tailored summary for this role.",
        highlights=["New one", "New two", "New three"],
    )
    soup = BeautifulSoup(render_resume(sample_profile(), tailored, TEMPLATE), "lxml")
    assert soup.find("div", class_="title").get_text(strip=True) == "Cloud Solutions Architect"
    assert "Tailored summary" in soup.find("p", class_="summary").get_text()
    items = [li.get_text(strip=True) for li in soup.find("ul", class_="highlights").find_all("li")]
    assert items == ["New one", "New two", "New three"]


def test_render_preserves_css_and_structure():
    """The layout was hand-tuned. Tailoring edits content, never presentation."""
    html = render_resume(sample_profile(), TailoredResume(
        headline="X", summary="Y", highlights=["Z"]), TEMPLATE)
    assert "font-size:9.4pt" in html
    soup = BeautifulSoup(html, "lxml")
    assert [h.get_text(strip=True) for h in soup.find_all("h2")] == [
        "Professional Summary", "Key Highlights", "Technical Skills",
        "Professional Experience", "Selected Projects",
    ]


def test_render_cannot_change_employer_or_dates():
    """The core safety property.

    `TailoredResume` has no field for org, title, or dates, so even a model that
    tried to rewrite an employer name has no channel to do it. The company line
    comes from the template/profile untouched.
    """
    tailored = TailoredResume(
        headline="X", summary="Y", highlights=["Z"],
        role_bullets={"Dakota Law Firm": ["Rewritten bullet about the same job"]},
    )
    soup = BeautifulSoup(render_resume(sample_profile(), tailored, TEMPLATE), "lxml")
    company_line = soup.find("div", class_="company-line").get_text(" ", strip=True)
    assert "Dakota Law Firm" in company_line
    assert "Jan 2025" in company_line and "Jan 2026" in company_line
    assert soup.find("h3").get_text(strip=True) == "Software Engineer"


def test_render_rewrites_role_bullets():
    tailored = TailoredResume(
        headline="X", summary="Y", highlights=["Z"],
        role_bullets={"Dakota Law Firm": ["Bullet A", "Bullet B"]},
    )
    soup = BeautifulSoup(render_resume(sample_profile(), tailored, TEMPLATE), "lxml")
    bullets = [
        li.get_text(strip=True)
        for li in soup.find("div", class_="company-line").find_next("ul").find_all("li")
    ]
    assert bullets == ["Bullet A", "Bullet B"]


def test_render_role_bullets_match_org_case_insensitively():
    tailored = TailoredResume(
        headline="X", summary="Y", highlights=["Z"],
        role_bullets={"dakota law firm": ["Matched anyway"]},
    )
    html = render_resume(sample_profile(), tailored, TEMPLATE)
    assert "Matched anyway" in html


def test_render_reorders_skills_without_dropping_any():
    """A partial ordering from the model must not silently lose a skill group."""
    tailored = TailoredResume(
        headline="X", summary="Y", highlights=["Z"], skill_group_order=["Cloud"]
    )
    soup = BeautifulSoup(render_resume(sample_profile(), tailored, TEMPLATE), "lxml")
    labels = [r.find(class_="skills-label").get_text(strip=True)
              for r in soup.find_all(class_="skills-row")]
    assert labels == ["Cloud", "Languages"]


def test_render_tolerates_empty_tailoring():
    html = render_resume(sample_profile(), TailoredResume(headline="", summary="",
                                                         highlights=[]), TEMPLATE)
    assert "Allan Kariuki" in html


# --- trimming ----------------------------------------------------------------


def test_trim_drops_trailing_bullet_of_longest_role():
    from src.generate import trim_for_one_page

    tailored = TailoredResume(
        headline="X", summary="Y", highlights=["a", "b", "c"],
        role_bullets={"A": ["1", "2", "3"], "B": ["1"]},
    )
    assert trim_for_one_page(tailored) is True
    assert tailored.role_bullets["A"] == ["1", "2"]
    assert tailored.role_bullets["B"] == ["1"]


def test_trim_eventually_gives_up():
    from src.generate import trim_for_one_page

    tailored = TailoredResume(headline="X", summary="short", highlights=["a", "b"],
                              role_bullets={"A": ["1"]})
    for _ in range(20):
        if not trim_for_one_page(tailored):
            break
    assert trim_for_one_page(tailored) is False


# --- retrieval ---------------------------------------------------------------


def test_tokenize_drops_stopwords_keeps_tech_tokens():
    tokens = tokenize("We use Node.js and C++ with the AWS API")
    assert "the" not in tokens and "with" not in tokens
    assert "node.js" in tokens
    assert "aws" in tokens


def test_bm25_ranks_relevant_doc_first():
    index = Index()
    index.add("a.md", "kubernetes helm docker container orchestration cluster")
    index.add("b.md", "neo4j graph database cypher graphrag knowledge graph")
    top = index.search("graph database cypher", limit=2)
    assert top and top[0][0] == "b.md"


def test_bm25_empty_index_returns_nothing():
    assert Index().search("anything") == []


def test_index_roundtrips_through_json():
    index = Index()
    index.add("a.md", "fastapi neo4j satellite ndvi", title="FarmWise")
    restored = Index.from_json(index.to_json())
    assert restored.search("neo4j satellite")[0][0] == "a.md"
    assert restored.titles["a.md"] == "FarmWise"


def test_slugify():
    assert slugify("The Dakota Law Firm PLLC") == "the-dakota-law-firm-pllc"
    assert slugify("AWS / Bedrock  Portfolio!") == "aws-bedrock-portfolio"


# --- real CV (skipped when the source file isn't present) --------------------

real_cv = pytest.mark.skipif(
    not config.CV_HTML.exists(), reason="real CV not present on this machine"
)


@real_cv
def test_real_cv_parses_expected_shape():
    profile, raw = parse_cv()
    assert profile.contact.name
    assert "@" in profile.contact.email
    assert profile.roles, "expected at least one role"
    assert profile.skills, "expected skill groups"
    assert len(raw) > 1000


@real_cv
def test_real_cv_dates_are_month_precision():
    profile, _ = parse_cv()
    import re

    for role in profile.roles:
        assert re.fullmatch(r"\d{4}-\d{2}", role.start), role.start
        assert role.end is None or re.fullmatch(r"\d{4}-\d{2}", role.end), role.end


@real_cv
def test_real_cv_roles_have_org_and_bullets():
    profile, _ = parse_cv()
    for role in profile.roles:
        assert role.org, f"role {role.title} has no org"
        assert role.bullets, f"role {role.title} has no bullets"


# --- markdown heading normalisation ------------------------------------------


def test_demote_headings_shifts_levels():
    from src.extract import demote_headings

    assert demote_headings("## Overview\ntext\n### Sub") == "### Overview\ntext\n#### Sub"


def test_demote_headings_leaves_prose_and_hashtags_alone():
    from src.extract import demote_headings

    assert demote_headings("no heading here\n#hashtag") == "no heading here\n#hashtag"


def test_demote_headings_ignores_code_fences():
    """A '# comment' inside a fenced block is code, not a heading."""
    from src.extract import demote_headings

    src = "## Real\n```bash\n# a shell comment\n```\n## Also real"
    assert demote_headings(src) == "### Real\n```bash\n# a shell comment\n```\n### Also real"
