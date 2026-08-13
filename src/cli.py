"""Command line entry point.

    jobapp ingest      build source-of-truth from the real CV and repos
    jobapp scrape      fetch and rank jobs into lead-gen/jobs.jsonl
    jobapp generate    match, tailor, and render packages into output/
    jobapp run         scrape + generate
    jobapp status      what exists right now
    jobapp web         start the web UI at http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import config

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


def _load_prefs():
    from .contracts import Preferences

    if not config.PREFERENCES_YAML.exists():
        raise typer.BadParameter(f"missing {config.PREFERENCES_YAML}")
    return Preferences(**(yaml.safe_load(config.PREFERENCES_YAML.read_text()) or {}))


# --- ingest ------------------------------------------------------------------


@app.command()
def ingest(
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Parse the CV only. Skips project docs and the audit."
    ),
    project: str = typer.Option(
        None, "--project", help="Rebuild a single project doc by slug."
    ),
):
    """Build source-of-truth from the CV and the real project repositories."""
    from . import extract
    from . import retrieve

    console.print(f"[bold]Parsing CV[/bold] {config.CV_HTML}")
    profile, raw_html = extract.parse_cv()

    for directory in (config.EXPERIENCE_DIR, config.PROJECTS_DIR, config.WRITING_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    config.TEMPLATE_HTML.write_text(raw_html, encoding="utf-8")
    console.print(f"  template  -> {config.TEMPLATE_HTML.name}")

    # Experience prose, one file per role.
    experience_docs = []
    for role in profile.roles:
        slug = extract.slugify(f"{role.org}-{role.title}")[:60]
        path = config.EXPERIENCE_DIR / f"{slug}.md"
        front = yaml.safe_dump(
            {
                "org": role.org, "title": role.title, "location": role.location,
                "start": role.start, "end": role.end,
            },
            sort_keys=False,
        ).strip()
        body = "\n".join(f"- {b}" for b in role.bullets)
        path.write_text(
            f"---\n{front}\n---\n\n# {role.title} — {role.org}\n\n{body}\n",
            encoding="utf-8",
        )
        experience_docs.append(str(path.relative_to(config.ROOT)))
    profile.experience_docs = experience_docs
    console.print(f"  experience-> {len(experience_docs)} role docs")

    # Writing samples, for cover-letter voice.
    samples = []
    if config.BLOGS_DIR.exists():
        for source in sorted(config.BLOGS_DIR.glob("*.md")):
            if config.is_excluded(source):
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            clean, hits = config.scrub(text)
            target = config.WRITING_DIR / f"{extract.slugify(source.stem)[:60]}.md"
            target.write_text(clean, encoding="utf-8")
            samples.append(str(target.relative_to(config.ROOT)))
            if hits:
                console.print(f"  [yellow]redacted secret-shaped text from {source.name}")
    profile.writing_samples = samples
    console.print(f"  voice     -> {len(samples)} writing sample(s)")

    if not no_llm:
        from .llm import get_client

        client = get_client()

        console.print("[bold]Deriving project architecture from code[/bold]")
        docs = asyncio.run(extract.build_projects(client, only=project))
        project_docs = []
        for doc in docs:
            markdown, hits = config.scrub(extract.project_doc_markdown(doc))
            if hits:
                console.print(f"  [yellow]redacted secret-shaped text from {doc.slug}")
            path = config.PROJECTS_DIR / f"{doc.slug}.md"
            path.write_text(markdown, encoding="utf-8")
            project_docs.append(str(path.relative_to(config.ROOT)))
            console.print(f"  {doc.slug:<24} <- {doc.origin}")
        if project:
            existing = [
                str(p.relative_to(config.ROOT))
                for p in sorted(config.PROJECTS_DIR.glob("*.md"))
            ]
            profile.project_docs = existing
        else:
            profile.project_docs = project_docs

        console.print("[bold]Auditing CV[/bold]")
        audit = asyncio.run(extract.audit_cv(client, profile))
        config.AUDIT_JSON.write_text(audit.model_dump_json(indent=2), encoding="utf-8")
        for flag in audit.flags:
            colour = {"high": "red", "medium": "yellow", "low": "dim"}[flag.severity]
            console.print(f"  [{colour}]{flag.severity:<6} {flag.kind}[/] {flag.detail}")
        console.print(f"  [dim]{client.usage.report()}")
    else:
        profile.project_docs = [
            str(p.relative_to(config.ROOT))
            for p in sorted(config.PROJECTS_DIR.glob("*.md"))
        ]
        flags = extract.audit_dates(profile.roles)
        for flag in flags:
            console.print(f"  [yellow]{flag.severity:<6} {flag.kind}[/] {flag.detail}")

    config.PROFILE_JSON.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    index = retrieve.build_index()
    retrieve.save_index(index)
    console.print(
        f"[green]done[/green] profile.json + BM25 index over {index.n_docs} docs"
    )


# --- scrape ------------------------------------------------------------------


@app.command()
def scrape(
    source: list[str] = typer.Option(
        None, "--source", "-s",
        help="Limit to these sources. Repeatable. 'apify' must be requested explicitly.",
    ),
    verify_boards: bool = typer.Option(
        False, "--verify-boards", help="Report which ATS board tokens resolved."
    ),
):
    """Fetch jobs from every configured source, rank them, keep the best."""
    from . import rank
    from . import sources

    prefs = _load_prefs()
    companies = yaml.safe_load(config.COMPANIES_YAML.read_text()) or {}
    enabled = set(source) if source else None

    console.print("[bold]Fetching[/bold]")
    srcs = sources.build_sources(companies, enabled=enabled, titles=prefs.titles)
    if not srcs:
        raise typer.Exit("no usable sources configured")
    jobs = asyncio.run(sources.fetch_all(srcs))
    console.print(f"  raw total: {len(jobs)}")

    kept, dropped = rank.process(jobs, prefs)

    config.LEAD_GEN.mkdir(parents=True, exist_ok=True)
    with config.JOBS_JSONL.open("w", encoding="utf-8") as handle:
        for job in kept:
            handle.write(job.model_dump_json() + "\n")

    from collections import Counter

    console.print(f"[green]kept {len(kept)}[/green], dropped {len(dropped)}")
    table = Table("source", "kept", box=None)
    for name, count in Counter(j.source for j in kept).most_common():
        table.add_row(name, str(count))
    console.print(table)

    reasons = Counter(reason.split(":")[0] for _, reason in dropped)
    console.print("[dim]drop reasons: " + ", ".join(
        f"{r} ({n})" for r, n in reasons.most_common(6)
    ))

    top = Table("pay", "salary", "remote", "company", "title", box=None)
    for job in kept[:15]:
        top.add_row(
            str(job.pay_score),
            f"${job.salary_usd_estimate:,}" if job.salary_usd_estimate else "—",
            job.remote_scope,
            job.company[:18],
            job.title[:46],
        )
    console.print(top)

    if verify_boards:
        console.print("[dim]board tokens that returned nothing are logged above")


# --- generate ----------------------------------------------------------------


@app.command()
def generate(
    limit: int = typer.Option(10, "--limit", "-n", help="How many packages to build."),
    pool: int = typer.Option(
        0, "--pool",
        help="How many jobs to score before choosing. Defaults to 5x limit. "
             "Scoring is one cheap call; building a package is six.",
    ),
    persona: str = typer.Option("senior-tech-recruiter", "--persona"),
    threshold: int = typer.Option(
        config.MATCH_THRESHOLD, "--threshold",
        help="Minimum match score to build a package. Lower = more packages, lower quality.",
    ),
):
    """Score a pool of jobs, then build packages for the best matches.

    Two phases on purpose. The queue is ranked by pay, and the highest-paying jobs
    are also the hardest to get — taking the top N by pay means building packages
    for the roles you are least likely to land. So score widely first, then spend
    the expensive generation calls on the jobs that actually scored well.
    """
    from . import generate as gen
    from . import retrieve
    from .llm import get_client

    profile = gen.load_profile()
    template_html = config.TEMPLATE_HTML.read_text(encoding="utf-8")
    persona_text = gen.load_persona(persona)
    voice = gen.load_voice()
    all_jobs = gen.load_jobs()

    pool_size = pool or min(len(all_jobs), max(limit * 5, limit))
    pool_jobs = all_jobs[:pool_size]

    index = retrieve.load_index()
    client = get_client()

    console.print(
        f"[bold]Scoring {len(pool_jobs)} job(s)[/bold] as '{persona}', "
        f"then building up to {limit} package(s)"
    )

    scored = asyncio.run(
        gen.score_pool(client, pool_jobs, profile, persona_text, index)
    )
    scored.sort(key=lambda t: (-t[1].score, -t[0].pay_score))

    buckets = {"80+": 0, "60-79": 0, "40-59": 0, "<40": 0}
    for _, match, _ in scored:
        key = ("80+" if match.score >= 80 else "60-79" if match.score >= 60
               else "40-59" if match.score >= 40 else "<40")
        buckets[key] += 1
    console.print("[dim]score distribution: " + "  ".join(
        f"{k} {v}" for k, v in buckets.items()
    ))

    eligible = [t for t in scored if t[1].score >= threshold][:limit]
    skipped = [t for t in scored if t[1].score < threshold]

    if not eligible:
        best = scored[0] if scored else None
        console.print(
            f"[yellow]no job in the pool scored >= {threshold}.[/yellow] "
            + (f"Best was {best[1].score} ({best[0].company} — {best[0].title}).\n"
               f"[dim]{best[1].rationale[:200]}" if best else "")
        )
        console.print("[dim]Try a larger --pool, or widen preferences.yaml — the top of "
                      "the pay-ranked queue skews to roles above this profile's level.")

    console.print(f"[bold]Building {len(eligible)} package(s)[/bold]")

    async def build_all():
        return await asyncio.gather(
            *(gen.build_package(client, job, match, projects_text, profile,
                                template_html, voice, threshold=threshold)
              for job, match, projects_text in eligible),
            return_exceptions=True,
        )

    results = asyncio.run(build_all()) if eligible else []

    packages = []
    for (job, match, _), result in zip(eligible, results):
        if isinstance(result, Exception):
            console.print(f"  [red]fail[/red] {job.company} — {job.title}: "
                          f"{type(result).__name__}: {result}")
            continue
        packages.append(result)
        colour = "green" if result.resume_pages == 1 else "yellow"
        console.print(
            f"  [{colour}]ok[/{colour}]  {result.match.score:>3} "
            f"{job.company[:18]:<18} {job.title[:40]:<40} "
            f"{result.resume_pages}p {' '.join(result.notes)[:50]}"
        )

    for job, match, _ in skipped[:8]:
        console.print(f"  [dim]skip {match.score:>3} {job.company[:18]:<18} "
                      f"{job.title[:40]}")
    if len(skipped) > 8:
        console.print(f"  [dim]... and {len(skipped) - 8} more below threshold")

    gen.write_manifest(packages)
    made = [p for p in packages if p.status != "skipped_low_score"]
    console.print(
        f"[green]{len(made)} package(s)[/green] in output/, queue at "
        f"{config.MANIFEST_CSV.relative_to(config.ROOT)}"
    )
    console.print(f"[dim]{client.usage.report()}")


@app.command()
def run(limit: int = typer.Option(10, "--limit", "-n")):
    """scrape + generate."""
    scrape(source=None, verify_boards=False)
    generate(limit=limit, persona="senior-tech-recruiter")


# --- status ------------------------------------------------------------------


@app.command()
def status():
    """What currently exists on disk."""
    table = Table("module", "state", box=None)

    if config.PROFILE_JSON.exists():
        profile = json.loads(config.PROFILE_JSON.read_text())
        table.add_row(
            "source-of-truth",
            f"{profile['contact']['name']}, {len(profile.get('roles', []))} roles, "
            f"{len(list(config.PROJECTS_DIR.glob('*.md')))} project docs, "
            f"{len(list(config.WRITING_DIR.glob('*.md')))} writing samples",
        )
    else:
        table.add_row("source-of-truth", "[red]not built — run `jobapp ingest`")

    if config.JOBS_JSONL.exists():
        lines = [l for l in config.JOBS_JSONL.read_text().splitlines() if l.strip()]
        jobs = [json.loads(l) for l in lines]
        explicit = sum(1 for j in jobs if j.get("salary_explicit"))
        worldwide = sum(1 for j in jobs if j.get("remote_scope") == "worldwide")
        table.add_row(
            "lead-gen",
            f"{len(jobs)} jobs, {explicit} with stated salary, {worldwide} remote-worldwide",
        )
    else:
        table.add_row("lead-gen", "[red]no jobs — run `jobapp scrape`")

    dirs = [d for d in config.OUTPUT.glob("*") if d.is_dir()]
    if dirs:
        one_page = 0
        for d in dirs:
            pkg = d / "package.json"
            if pkg.exists() and json.loads(pkg.read_text()).get("resume_pages") == 1:
                one_page += 1
        table.add_row("output", f"{len(dirs)} packages, {one_page} with a 1-page resume")
    else:
        table.add_row("output", "[red]no packages — run `jobapp generate`")

    if config.MANIFEST_CSV.exists():
        rows = config.MANIFEST_CSV.read_text().splitlines()[1:]
        applied = sum(1 for r in rows if r.split(",")[-2:-1] not in ([], [""]))
        table.add_row("outbound", f"{len(rows)} queued, {applied} marked applied "
                                  f"(manual by design)")
    else:
        table.add_row("outbound", "no manifest yet")

    console.print(table)

    if config.AUDIT_JSON.exists():
        audit = json.loads(config.AUDIT_JSON.read_text())
        flags = audit.get("flags", [])
        if flags:
            console.print(f"\n[bold]CV flags[/bold] ({len(flags)})")
            for flag in flags[:8]:
                console.print(f"  {flag['severity']:<6} {flag['kind']}: {flag['detail'][:110]}")


@app.command()
def web(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address."),
    port: int = typer.Option(8000, "--port", help="Port to serve on."),
):
    """Start the Apple-styled web UI."""
    import uvicorn

    console.print(f"[bold]Job App System[/bold] — http://{host}:{port}")
    uvicorn.run("src.web:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()