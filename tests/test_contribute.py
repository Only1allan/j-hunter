"""Open-source contribution targeting.

Network-free: every test here covers the judgement calls — which org, which repo,
which issue — not the HTTP plumbing.
"""

import pytest

from src.contribute import (
    Opportunity,
    RepoSignals,
    languages_from_profile,
    org_candidates,
    render_markdown,
)


# --- org resolution ----------------------------------------------------------


def test_generic_suffix_is_tried_without_it():
    """Grafana Labs is `grafana` on GitHub. `grafana-labs` also exists but is
    empty, so a first-match rule reports zero repos for a company with hundreds."""
    candidates = org_candidates("Grafana Labs")
    assert "grafana" in candidates
    assert candidates.index("grafana") > candidates.index("grafanalabs"), (
        "the literal name should still be tried first"
    )


def test_explicit_org_short_circuits_guessing():
    assert org_candidates("Anything At All", github_org="clickhouse") == ["clickhouse"]


def test_candidates_cover_both_joining_conventions():
    candidates = org_candidates("Sword Health")
    assert "swordhealth" in candidates and "sword-health" in candidates


def test_unusable_names_produce_nothing():
    assert org_candidates("!!!") == []


# --- welcome scoring ---------------------------------------------------------


def test_archived_repo_scores_zero_regardless_of_popularity():
    """A pull request against an archived repo cannot be merged, so recommending
    it costs real effort for a guaranteed dead end."""
    repo = RepoSignals(
        full_name="acme/famous", stars=50_000, has_contributing=True,
        open_issues=200, archived=True,
    )
    assert repo.welcome_score() == 0


def test_contributing_file_is_the_strongest_signal():
    with_doc = RepoSignals(full_name="a/b", has_contributing=True)
    without = RepoSignals(full_name="a/b", has_contributing=False)
    assert with_doc.welcome_score() > without.welcome_score()


def test_recent_activity_lifts_the_score():
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat()
    assert (RepoSignals(full_name="a/b", pushed_at=recent).welcome_score()
            > RepoSignals(full_name="a/b", pushed_at=stale).welcome_score())


def test_unparseable_push_date_does_not_raise():
    assert RepoSignals(full_name="a/b", pushed_at="not-a-date").welcome_score() >= 0


def test_score_is_bounded():
    from datetime import datetime, timezone

    repo = RepoSignals(
        full_name="a/b", stars=99_999, has_contributing=True, open_issues=500,
        pushed_at=datetime.now(timezone.utc).isoformat(),
    )
    assert 0 <= repo.welcome_score() <= 100


# --- opportunity classification ----------------------------------------------


@pytest.mark.parametrize("labels,expected", [
    (["good first issue"], True),
    (["Good First Issue"], True),
    (["help wanted", "bug"], True),
    (["up-for-grabs"], True),
    (["bug", "p1"], False),
    ([], False),
])
def test_newcomer_friendly_detection(labels, expected):
    opp = Opportunity(repo="a/b", number=1, title="t", url="u", kind="issue",
                      labels=labels)
    assert opp.is_newcomer_friendly is expected


# --- rendering ---------------------------------------------------------------


def test_markdown_names_repos_and_issues():
    research = {
        "org": "clickhouse",
        "repos": [RepoSignals(full_name="ClickHouse/clickhouse-js", stars=327,
                              language="TypeScript", has_contributing=True,
                              open_issues=12)],
        "opportunities": [Opportunity(
            repo="ClickHouse/clickhouse-js", number=42, title="Fix retry backoff",
            url="https://github.com/ClickHouse/clickhouse-js/issues/42",
            kind="issue", labels=["good first issue"], comments=3,
            updated_at="2026-08-01",
        )],
    }
    md = render_markdown("ClickHouse", research)
    assert "clickhouse-js" in md
    assert "Fix retry backoff" in md
    assert "good first issue" in md.lower()
    assert "#42" in md


def test_markdown_is_useful_when_there_is_no_github_org():
    """A company with no public org is common and is not an error — the artifact
    still has to tell the user what to do instead."""
    md = render_markdown("Acme", {"org": "", "repos": [], "opportunities": []})
    assert "No public GitHub organisation" in md
    assert "private repositories" in md


def test_markdown_handles_org_with_no_matching_repos():
    md = render_markdown("Acme", {"org": "acme", "repos": [], "opportunities": []})
    assert "acme" in md
    assert "No public repositories matched" in md


# --- language mapping --------------------------------------------------------


def test_languages_are_mapped_to_github_spelling():
    class FakeProfile:
        def all_skills(self):
            return ["python", "TypeScript", "Golang", "Terraform", "Kubernetes"]

    languages = languages_from_profile(FakeProfile())
    assert languages[:3] == ["Python", "TypeScript", "Go"]
    assert "HCL" in languages, "Terraform is HCL on GitHub"
    assert "Kubernetes" not in languages, "not a language"


def test_language_list_deduplicates():
    class FakeProfile:
        def all_skills(self):
            return ["Go", "golang", "GO"]

    assert languages_from_profile(FakeProfile()) == ["Go"]
