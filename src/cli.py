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
        help="Limit to these sources. Repeatable. Kenyan crawlers (fuzu, "
             "myjobmag, brightermonday) and 'apify' are off by default and must "
             "be requested explicitly.",
    ),
    verify_boards: bool = typer.Option(
        False, "--verify-boards", help="Report which ATS board tokens resolved."
    ),
):
    """Fetch jobs from every configured source, rank them, keep the best."""
    _scrape(source=source, verify_boards=verify_boards)


def _scrape(source: list[str] | None = None, verify_boards: bool = False) -> None:
    """The actual implementation, with real Python defaults.

    Kept separate from the typer command because calling a command function
    directly (as `run` does) leaves any omitted parameter bound to a typer
    `OptionInfo` sentinel rather than its default value. `OptionInfo` is truthy
    and is not an int, so it survives an `or` and then explodes at first use.
    """
    from . import rank
    from . import sources

    prefs = _load_prefs()
    companies = yaml.safe_load(config.COMPANIES_YAML.read_text()) or {}
    enabled = set(source) if source else None

    if enabled:
        unknown = enabled - sources.ALL_SOURCES
        if unknown:
            raise typer.BadParameter(
                f"unknown source(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(sources.ALL_SOURCES))}"
            )

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
    _generate(limit=limit, pool=pool, persona=persona, threshold=threshold)


def _generate(
    limit: int = 10,
    pool: int = 0,
    persona: str = "senior-tech-recruiter",
    threshold: int = config.MATCH_THRESHOLD,
) -> None:
    """The actual implementation. See `_scrape` for why this is split out."""
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

    # Contacts and languages, so the outreach email knows who it is addressing and
    # the contribution finder knows which repos are relevant. Both are optional —
    # a package still builds without either.
    from . import contribute
    from . import enrich as en
    from .rank import normalize_company

    employers = {e.slug: e for e in en.read_employers()}
    languages = contribute.languages_from_profile(profile)
    if employers:
        console.print(f"[dim]{len(employers)} enriched employer(s) available for "
                      f"outreach addressing")

    async def build_all():
        return await asyncio.gather(
            *(gen.build_package(client, job, match, projects_text, profile,
                                template_html, voice, threshold=threshold,
                                employer=employers.get(normalize_company(job.company)),
                                languages=languages)
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


@app.command("discover-boards")
def discover_boards(
    seed: Path = typer.Option(
        None, "--seed", help="YAML/text file of company names. Defaults to "
                             "lead-gen/seed-companies.yaml.",
    ),
    company: list[str] = typer.Option(
        None, "--company", "-c", help="Probe a single company name. Repeatable."
    ),
    write: bool = typer.Option(
        False, "--write", help="Merge discovered tokens into companies.yaml."
    ),
    ats: list[str] = typer.Option(
        None, "--ats", help="Limit the probe to these ATS platforms."
    ),
):
    """Find which ATS each employer publishes on, and verify the board is live.

    Company names go in, verified board tokens come out. Nothing is guessed:
    every reported token returned a real posting count from a live board.
    """
    from . import discover as disc
    from .sources import ATS_SOURCES

    names = list(company or [])
    if not names:
        path = seed or disc.SEED_PATH
        if not path.exists():
            raise typer.BadParameter(f"no seed file at {path}")
        names = disc.load_seed(path)

    ats_names = [a for a in (ats or []) if a in ATS_SOURCES] or list(ATS_SOURCES)
    console.print(
        f"[bold]Probing {len(names)} companies[/bold] across {len(ats_names)} ATS "
        f"platforms ({', '.join(ats_names)})"
    )

    hits = asyncio.run(disc.discover(names, ats_names))
    hits.sort(key=lambda h: (-h.postings, h.company.lower()))

    table = Table("company", "ats", "token", "postings", box=None)
    for hit in hits:
        table.add_row(hit.company, hit.ats, hit.token, str(hit.postings))
    console.print(table)

    found = {h.company for h in hits}
    missing = [n for n in names if n not in found]
    console.print(
        f"[green]{len(hits)} board(s) found[/green] carrying "
        f"{sum(h.postings for h in hits):,} postings; "
        f"{len(missing)} company(ies) had no reachable public board"
    )
    if missing:
        console.print(f"[dim]no board: {', '.join(missing[:20])}"
                      + (" ..." if len(missing) > 20 else ""))

    from collections import Counter

    spread = Counter(h.ats for h in hits)
    console.print("[dim]by ATS: " + ", ".join(f"{a} {n}" for a, n in spread.most_common()))

    if write:
        existing = yaml.safe_load(config.COMPANIES_YAML.read_text()) or {}
        merged = disc.merge_into_companies(hits, existing)
        header = (
            "# Target companies, keyed by the ATS hosting their public job board.\n"
            "#\n"
            "# Maintained by `jobapp discover-boards --write`, which probes every\n"
            "# supported ATS for each name in seed-companies.yaml and keeps only the\n"
            "# boards that actually answered. Hand edits are preserved on merge.\n"
            "#\n"
            "# Curating this list is the highest-leverage tuning in the pipeline: it\n"
            "# decides which companies you apply to at all.\n\n"
        )
        config.COMPANIES_YAML.write_text(
            header + yaml.safe_dump(merged, sort_keys=True, default_flow_style=False),
            encoding="utf-8",
        )
        total = sum(len(v) for v in merged.values())
        console.print(f"[green]wrote[/green] {total} token(s) to "
                      f"{config.COMPANIES_YAML.relative_to(config.ROOT)}")
    else:
        console.print("[dim]re-run with --write to merge these into companies.yaml")


@app.command()
def enrich(
    limit: int = typer.Option(40, "--limit", "-n", help="How many employers."),
    no_guess: bool = typer.Option(
        False, "--no-guess",
        help="Only report addresses actually observed on the employer's site.",
    ),
):
    """Find a careers contact for each employer behind the scraped jobs.

    Writes lead-gen/employers.jsonl. Every contact records where it came from and
    how confident that makes it, because a `careers@` address read off a live
    careers page and the same string generated from convention are not the same
    fact — and only one of them is safe to send to unreviewed.
    """
    from . import enrich as en
    from . import generate as gen

    jobs = gen.load_jobs()
    employers = en.employers_from_jobs(jobs)
    employers.sort(key=lambda e: -e.open_roles)
    employers = employers[:limit]

    console.print(f"[bold]Enriching {len(employers)} employer(s)[/bold] "
                  f"from {len(jobs)} job(s) — MX verification only, no SMTP probing")

    enriched = asyncio.run(en.enrich_all(employers, guess=not no_guess))
    en.write_employers(enriched)

    table = Table("employer", "roles", "domain", "dom?", "best contact", "conf",
                  "source", box=None)
    observed = 0
    for employer in enriched:
        contact = employer.best_contact()
        if contact and contact.source != "rfc2142_guess":
            observed += 1
        table.add_row(
            employer.name[:20], str(employer.open_roles), employer.domain[:24],
            ("[green]ok" if employer.domain_source == "posting"
             else "[yellow]guess"),
            (contact.email[:32] if contact else "—"),
            (f"{contact.confidence:.2f}" if contact else "—"),
            (contact.source if contact else "—"),
        )
    console.print(table)
    console.print(
        f"[green]{observed}[/green] employer(s) with an address actually published "
        f"on their site; the rest are conventions and are held for review.\n"
        f"[dim]wrote {config.EMPLOYERS_JSONL.relative_to(config.ROOT)}"
    )


@app.command()
def outreach(
    limit: int = typer.Option(20, "--limit", "-n"),
    min_confidence: float = typer.Option(
        0.5, "--min-confidence",
        help="Below this, a contact is held for review rather than queued.",
    ),
):
    """Draft an outreach email per generated package. Sends nothing.

    Each draft is written as a `.eml` you open in your own mail client. Anything
    the AI could not resolve — a missing contact, a guessed address, a
    [NEEDS YOUR INPUT] marker — goes to outbound/needs-review/ instead, with the
    reason recorded.
    """
    from . import enrich as en
    from . import generate as gen
    from . import outbound as ob

    profile = gen.load_profile()
    employers = {e.slug: e for e in en.read_employers()}
    do_not_contact = en.load_do_not_contact()

    from .rank import normalize_company

    items: list[ob.QueueItem] = []
    for pkg_dir in sorted(d for d in config.OUTPUT.glob("*") if d.is_dir()):
        pkg_path = pkg_dir / "package.json"
        email_path = pkg_dir / "outreach-email.md"
        if not pkg_path.exists():
            continue
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        job = pkg.get("job", {})
        raw = email_path.read_text(encoding="utf-8") if email_path.exists() else ""
        subject, body = gen.split_email(raw)
        answers = (pkg_dir / "answers.md")
        items.append(ob.build_item(
            slug=pkg_dir.name,
            company=job.get("company", ""),
            role=job.get("title", ""),
            apply_url=job.get("apply_url", ""),
            subject=subject, body=body,
            employer=employers.get(normalize_company(job.get("company", ""))),
            package_dir=pkg_dir,
            answers_text=answers.read_text(encoding="utf-8") if answers.exists() else "",
            do_not_contact=do_not_contact,
            min_confidence=min_confidence,
        ))
        if len(items) >= limit:
            break

    if not items:
        console.print("[yellow]no packages found — run `jobapp generate` first")
        raise typer.Exit(1)

    result = ob.queue_items(items, profile.contact.name, profile.contact.email)

    table = Table("status", "company", "role", "to", "why", box=None)
    for item in items:
        colour = "green" if item.status == "ready" else "yellow"
        table.add_row(
            f"[{colour}]{item.status}[/{colour}]", item.company[:20], item.role[:34],
            item.to_email[:28] or "—",
            ", ".join(item.blockers)[:40] or "",
        )
    console.print(table)
    console.print(
        f"[green]{result['ready']} draft(s)[/green] in outbound/drafts/, "
        f"[yellow]{result['needs_review']}[/yellow] held in "
        f"{config.NEEDS_REVIEW_DIR.relative_to(config.ROOT)}/\n"
        f"[dim]Nothing was sent. Open a .eml in your mail client, attach the "
        f"listed files, and send it yourself."
    )


@app.command("export")
def export_snapshot(
    out: Path = typer.Option(
        None, "--out", "-o",
        help="Destination directory. Defaults to exports/<timestamp>/.",
    ),
    label: str = typer.Option(
        "", "--label", "-l",
        help="Tag the snapshot, e.g. --label floor-25k. Recorded in the manifest.",
    ),
    segment: str = typer.Option(
        None, "--segment", help="Export only one segment, e.g. --segment kenya."
    ),
    jobs_only: bool = typer.Option(
        False, "--jobs-only", help="Skip employers and the outbound queue."
    ),
):
    """Snapshot the current queue before re-scraping with different parameters.

    `scrape` rewrites lead-gen/jobs.jsonl in place, so a second run with a
    different floor or a different source set destroys the queue you were working
    from — and with it any ability to compare the two. This copies the current
    state somewhere durable first, in both JSONL (exact, re-importable) and CSV
    (openable in a spreadsheet), alongside the settings that produced it.
    """
    import csv as csvmod
    import shutil
    from datetime import datetime

    from . import generate as gen

    if not config.JOBS_JSONL.exists():
        console.print("[red]no lead-gen/jobs.jsonl — run `jobapp scrape` first")
        raise typer.Exit(1)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    name = f"{stamp}-{label}" if label else stamp
    dest = Path(out) if out else (config.ROOT / "exports" / name)
    dest.mkdir(parents=True, exist_ok=True)

    jobs = gen.load_jobs()
    if segment:
        jobs = [j for j in jobs if j.segment == segment]
        if not jobs:
            console.print(f"[yellow]no jobs in segment {segment!r}")
            raise typer.Exit(1)

    # JSONL: lossless, and can be dropped straight back into lead-gen/ later.
    with (dest / "jobs.jsonl").open("w", encoding="utf-8") as handle:
        for job in jobs:
            handle.write(job.model_dump_json() + "\n")

    # CSV: the format you can actually sort and annotate. The description is left
    # out — it is thousands of characters and makes the sheet unreadable.
    columns = [
        "fit_score", "pay_score", "skill_score", "segment", "company", "title",
        "salary_usd_estimate", "salary_currency", "salary_explicit",
        "remote_scope", "location", "source", "posted_at", "apply_url",
        "pay_rationale", "skill_rationale",
    ]
    with (dest / "jobs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csvmod.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            row = job.model_dump()
            row["pay_rationale"] = " | ".join(row.get("pay_rationale") or [])
            row["skill_rationale"] = " | ".join(row.get("skill_rationale") or [])
            writer.writerow({c: row.get(c, "") for c in columns})

    copied = ["jobs.jsonl", "jobs.csv"]

    # The settings that produced this queue. Without them a snapshot is a list of
    # jobs with no way to know what question it answered.
    for source_file in (config.PREFERENCES_YAML, config.IEP_YAML,
                        config.COMPANIES_YAML):
        if source_file.exists():
            shutil.copy2(source_file, dest / source_file.name)
            copied.append(source_file.name)

    if not jobs_only:
        for source_file in (config.EMPLOYERS_JSONL, config.MANIFEST_CSV,
                            config.QUEUE_JSONL):
            if source_file.exists():
                shutil.copy2(source_file, dest / source_file.name)
                copied.append(source_file.name)

    from collections import Counter

    segments = Counter(j.segment or "-" for j in jobs)
    with_salary = sum(1 for j in jobs if j.salary_usd_estimate)
    summary = {
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label,
        "jobs": len(jobs),
        "segments": dict(segments),
        "with_stated_salary": with_salary,
        "salary_floor_usd": _load_prefs().salary.floor_usd,
        "sources": dict(Counter(j.source for j in jobs)),
        "files": copied,
    }
    (dest / "snapshot.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    console.print(f"[green]exported {len(jobs)} job(s)[/green] to "
                  f"{dest.relative_to(config.ROOT) if dest.is_relative_to(config.ROOT) else dest}")
    table = Table("file", "what", box=None)
    for item in copied + ["snapshot.json"]:
        table.add_row(item, {
            "jobs.jsonl": "lossless — copy back to lead-gen/ to restore",
            "jobs.csv": "spreadsheet view, descriptions omitted",
            "preferences.yaml": "the filters that produced this queue",
            "iep.yaml": "employer profile in force",
            "companies.yaml": "ATS board tokens in force",
            "employers.jsonl": "contacts found for these employers",
            "manifest.csv": "application tracker, with your applied_on column",
            "queue.jsonl": "drafted outreach",
            "snapshot.json": "counts and settings, for comparing runs",
        }.get(item, ""))
    console.print(table)
    console.print(f"[dim]segments: {dict(segments)} · {with_salary} with stated salary\n"
                  f"[dim]Now edit preferences.yaml and re-run `jobapp scrape`; "
                  f"restore with `cp {dest.name}/jobs.jsonl lead-gen/`")


@app.command()
def run(limit: int = typer.Option(10, "--limit", "-n")):
    """scrape + generate."""
    _scrape()
    _generate(limit=limit)


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
        # Parsed properly, not by splitting on commas: job titles routinely contain
        # them ("Software Engineer, Payments"), which shifted every column and made
        # the applied count meaningless.
        from . import generate as gen

        rows = gen.read_manifest()
        applied = sum(1 for r in rows.values() if (r.get("applied_on") or "").strip())
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
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Defaults to localhost: the UI serves your CV, phone "
             "number and full application history, and has no authentication. "
             "Pass --host 0.0.0.0 only if you intend to expose it.",
    ),
    port: int = typer.Option(8000, "--port", help="Port to serve on."),
):
    """Start the Apple-styled web UI."""
    import uvicorn

    console.print(f"[bold]Job App System[/bold] — http://{host}:{port}")
    uvicorn.run("src.web:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()