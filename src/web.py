"""FastAPI web server. Wraps the existing pipeline as a REST API and serves
the Apple-styled frontend from web/.

    uv run jobapp web     # http://localhost:8000
"""

from __future__ import annotations

import csv
import json
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse

from . import config

app = FastAPI(title="Job Application System")

WEB_DIR = config.ROOT / "web"


@app.get("/")
def index():
    path = WEB_DIR / "index.html"
    if path.exists():
        return Response(content=path.read_bytes(), media_type="text/html; charset=utf-8")
    return {"error": "web/index.html not found"}


@app.get("/styles.css")
def styles():
    p = WEB_DIR / "styles.css"
    if p.exists():
        return Response(content=p.read_bytes(), media_type="text/css; charset=utf-8")
    raise HTTPException(404)


@app.get("/app.js")
def javascript():
    p = WEB_DIR / "app.js"
    if p.exists():
        return Response(content=p.read_bytes(), media_type="application/javascript; charset=utf-8")
    raise HTTPException(404)


# --- API: Status ------------------------------------------------------------


@app.get("/api/status")
def api_status():
    """Pipeline status — mirrors `jobapp status`."""
    result: dict = {}

    if config.PROFILE_JSON.exists():
        profile = json.loads(config.PROFILE_JSON.read_text(encoding="utf-8"))
        result["source_of_truth"] = {
            "name": profile["contact"]["name"],
            "roles": len(profile.get("roles", [])),
            "project_docs": len(list(config.PROJECTS_DIR.glob("*.md"))),
            "writing_samples": len(list(config.WRITING_DIR.glob("*.md"))),
        }
    else:
        result["source_of_truth"] = None

    if config.JOBS_JSONL.exists():
        lines = [l for l in config.JOBS_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
        jobs = [json.loads(l) for l in lines]
        result["lead_gen"] = {
            "total": len(jobs),
            "with_salary": sum(1 for j in jobs if j.get("salary_explicit")),
            "remote_worldwide": sum(1 for j in jobs if j.get("remote_scope") == "worldwide"),
        }
    else:
        result["lead_gen"] = None

    dirs = [d for d in config.OUTPUT.glob("*") if d.is_dir()]
    if dirs:
        one_page = 0
        for d in dirs:
            pkg = d / "package.json"
            if pkg.exists() and json.loads(pkg.read_text(encoding="utf-8")).get("resume_pages") == 1:
                one_page += 1
        result["output"] = {"packages": len(dirs), "one_page": one_page}
    else:
        result["output"] = None

    if config.MANIFEST_CSV.exists():
        rows = config.MANIFEST_CSV.read_text(encoding="utf-8").splitlines()[1:]
        applied = sum(1 for r in rows if r.strip() and r.split(",")[-2:-1] not in ([], [""]))
        result["outbound"] = {"queued": len([r for r in rows if r.strip()]), "applied": applied}
    else:
        result["outbound"] = None

    if config.AUDIT_JSON.exists():
        audit = json.loads(config.AUDIT_JSON.read_text(encoding="utf-8"))
        flags = audit.get("flags", [])
        result["audit"] = {
            "total_flags": len(flags),
            "high": sum(1 for f in flags if f.get("severity") == "high"),
            "medium": sum(1 for f in flags if f.get("severity") == "medium"),
            "low": sum(1 for f in flags if f.get("severity") == "low"),
            "strengths": len(audit.get("strengths", [])),
            "weaknesses": len(audit.get("weaknesses", [])),
        }
    else:
        result["audit"] = None

    return result


# --- API: Profile -----------------------------------------------------------


@app.get("/api/profile")
def api_profile():
    if not config.PROFILE_JSON.exists():
        raise HTTPException(404, "profile.json not found — run `jobapp ingest`")
    return json.loads(config.PROFILE_JSON.read_text(encoding="utf-8"))


@app.get("/api/profile/projects")
def api_profile_projects():
    """Project architecture docs from source-of-truth/projects/."""
    if not config.PROJECTS_DIR.exists():
        return {"projects": []}
    projects = []
    for path in sorted(config.PROJECTS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), path.stem)
        one_liner = ""
        for l in lines:
            if l.startswith("> "):
                one_liner = l.lstrip("> ").strip()
                break
        projects.append({
            "slug": path.stem,
            "title": title,
            "one_liner": one_liner,
            "content": text[:8000],
        })
    return {"projects": projects}


# --- API: Audit -------------------------------------------------------------


@app.get("/api/audit")
def api_audit():
    if not config.AUDIT_JSON.exists():
        raise HTTPException(404, "cv-audit.json not found — run `jobapp ingest`")
    return json.loads(config.AUDIT_JSON.read_text(encoding="utf-8"))


# --- API: Jobs --------------------------------------------------------------


@app.get("/api/jobs")
def api_jobs(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    source: str | None = None,
    remote: str | None = None,
    min_pay: int | None = None,
    search: str | None = None,
):
    if not config.JOBS_JSONL.exists():
        raise HTTPException(404, "jobs.jsonl not found — run `jobapp scrape`")

    lines = [l for l in config.JOBS_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
    jobs = [json.loads(l) for l in lines]

    if source:
        jobs = [j for j in jobs if j.get("source") == source]
    if remote:
        jobs = [j for j in jobs if j.get("remote_scope") == remote]
    if min_pay is not None:
        jobs = [j for j in jobs if j.get("pay_score", 0) >= min_pay]
    if search:
        s = search.lower()
        jobs = [j for j in jobs if s in j.get("company", "").lower() or s in j.get("title", "").lower()]

    total = len(jobs)
    page = jobs[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "jobs": page}


# --- API: Packages ----------------------------------------------------------


@app.get("/api/packages")
def api_packages():
    dirs = sorted(d for d in config.OUTPUT.glob("*") if d.is_dir())
    packages = []
    for d in dirs:
        pkg_path = d / "package.json"
        if pkg_path.exists():
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            pkg["slug"] = d.name
            packages.append(pkg)
    return {"total": len(packages), "packages": packages}


@app.get("/api/packages/{slug}")
def api_package(slug: str):
    pkg_dir = config.OUTPUT / slug
    if not pkg_dir.is_dir():
        raise HTTPException(404, f"package '{slug}' not found")

    pkg_path = pkg_dir / "package.json"
    if not pkg_path.exists():
        raise HTTPException(404, f"package.json not found in '{slug}'")

    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))

    artifacts: dict[str, str] = {}
    for name in ("cover-letter.md", "post.md", "study-plan.md", "answers.md", "job.md"):
        path = pkg_dir / name
        if path.exists():
            artifacts[name] = path.read_text(encoding="utf-8", errors="replace")

    pkg["slug"] = slug
    pkg["artifact_texts"] = artifacts
    return pkg


@app.get("/api/packages/{slug}/{filename}")
def api_package_file(slug: str, filename: str):
    allowed = {"resume.pdf", "resume.html", "cover-letter.pdf"}
    if filename not in allowed:
        raise HTTPException(403, f"file '{filename}' not allowed")
    path = config.OUTPUT / slug / filename
    if not path.exists():
        raise HTTPException(404, f"'{filename}' not found in package '{slug}'")
    media = "application/pdf" if filename.endswith(".pdf") else "text/html; charset=utf-8"
    return Response(content=path.read_bytes(), media_type=media)


# --- API: Outbound ----------------------------------------------------------


@app.get("/api/outbound")
def api_outbound():
    if not config.MANIFEST_CSV.exists():
        return {"rows": []}
    rows = []
    with config.MANIFEST_CSV.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return {"rows": rows}


# --- API: Run pipeline stages -----------------------------------------------
# Runs `jobapp <stage>` as a subprocess so it gets its own event loop.

_processes: dict[str, subprocess.Popen] = {}
_statuses: dict[str, str] = {}


@app.post("/api/run/{stage}")
def api_run(
    stage: str,
    limit: int | None = None,
    pool: int | None = None,
):
    if stage not in ("ingest", "scrape", "generate"):
        raise HTTPException(400, f"unknown stage: {stage}")

    existing = _processes.get(stage)
    if existing and existing.poll() is None:
        raise HTTPException(409, f"{stage} is already running")

    cmd = ["uv", "run", "jobapp", stage]
    if stage == "generate" and limit is not None:
        cmd += ["-n", str(limit)]
    if stage == "generate" and pool is not None:
        cmd += ["--pool", str(pool)]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(config.ROOT),
    )
    _processes[stage] = proc
    _statuses[stage] = "running"

    def _wait():
        proc.wait()
        if proc.returncode == 0:
            _statuses[stage] = "done"
        else:
            err = proc.stderr.read().decode(errors="replace")[-300:] if proc.stderr else ""
            _statuses[stage] = f"error (exit {proc.returncode}): {err}"

    threading.Thread(target=_wait, daemon=True).start()
    return {"stage": stage, "status": "running"}


@app.get("/api/run/{stage}/status")
def api_run_status(stage: str):
    if stage not in ("ingest", "scrape", "generate"):
        raise HTTPException(400, f"unknown stage: {stage}")
    proc = _processes.get(stage)
    if proc is None:
        return {"stage": stage, "status": "idle"}
    if proc.poll() is None:
        return {"stage": stage, "status": "running"}
    return {"stage": stage, "status": _statuses.get(stage, "done")}
