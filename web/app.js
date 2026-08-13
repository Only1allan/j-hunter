/* Job Application System — Apple-styled frontend.
   Vanilla JS, hash-based routing, no build step. */

const $ = (id) => document.getElementById(id);
const content = () => $('content');

async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) {
        const e = new Error(`${r.status} ${r.statusText}`);
        e.status = r.status;
        throw e;
    }
    return r.json();
}

function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

function inlineFmt(text) {
    let t = esc(text);
    t = t.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    return t;
}

function md(text) {
    if (!text) return '<p><em>Not available</em></p>';
    const lines = String(text).split('\n');
    let html = '', inList = false, inPara = false;
    for (const line of lines) {
        if (line.startsWith('### ')) { if(inList){html+='</ul>';inList=false} if(inPara){html+='</p>';inPara=false} html += `<h3>${inlineFmt(line.slice(4))}</h3>`; }
        else if (line.startsWith('## ')) { if(inList){html+='</ul>';inList=false} if(inPara){html+='</p>';inPara=false} html += `<h2>${inlineFmt(line.slice(3))}</h2>`; }
        else if (line.startsWith('# ')) { if(inList){html+='</ul>';inList=false} if(inPara){html+='</p>';inPara=false} html += `<h1>${inlineFmt(line.slice(2))}</h1>`; }
        else if (line.startsWith('- ')) { if(inPara){html+='</p>';inPara=false} if(!inList){html+='<ul>';inList=true} html += `<li>${inlineFmt(line.slice(2))}</li>`; }
        else if (line.startsWith('> ')) { if(inList){html+='</ul>';inList=false} if(inPara){html+='</p>';inPara=false} html += `<blockquote>${inlineFmt(line.slice(2))}</blockquote>`; }
        else if (line.trim() === '') { if(inList){html+='</ul>';inList=false} if(inPara){html+='</p>';inPara=false} }
        else { if(inList){html+='</ul>';inList=false} if(!inPara){html+='<p>';inPara=true} html += inlineFmt(line) + ' '; }
    }
    if (inList) html += '</ul>';
    if (inPara) html += '</p>';
    return html;
}

function scoreClass(s) { return s >= 80 ? 'score-high' : s >= 60 ? 'score-mid' : 'score-low'; }

function navHighlight(route) {
    document.querySelectorAll('.nav-item').forEach(a => a.classList.remove('active'));
    const link = document.querySelector(`.nav-item[href="#/${route}"]`);
    if (link) link.classList.add('active');
}

function fmtSalary(j) {
    if (!j.salary_min) return j.salary_usd_estimate ? `~$${j.salary_usd_estimate.toLocaleString()}` : '—';
    const cur = j.salary_currency || 'USD';
    let s = `${cur} ${j.salary_min.toLocaleString()}`;
    if (j.salary_max) s += `–${j.salary_max.toLocaleString()}`;
    return s;
}

// --- Dashboard ---

async function renderDashboard() {
    navHighlight('');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const s = await fetchJSON('/api/status');
        const sot = s.source_of_truth;
        const lg = s.lead_gen;
        const op = s.output;
        const ob = s.outbound;
        const au = s.audit;

        content().innerHTML = `
            <h1 class="section-title">Dashboard</h1>
            <div class="grid grid-4">
                <div class="stat">
                    <div class="stat-label">Source of Truth</div>
                    <div class="stat-value">${sot ? sot.roles : '—'}</div>
                    <div class="stat-sub">${sot ? sot.name : 'not built'} · ${sot ? sot.project_docs : 0} projects</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Jobs Scraped</div>
                    <div class="stat-value">${lg ? lg.total : '—'}</div>
                    <div class="stat-sub">${lg ? lg.with_salary + ' with salary' : 'not scraped'} · ${lg ? lg.remote_worldwide : 0} worldwide</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Packages Built</div>
                    <div class="stat-value">${op ? op.packages : '—'}</div>
                    <div class="stat-sub">${op ? op.one_page + ' one-page' : 'none yet'}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Outbound Queue</div>
                    <div class="stat-value">${ob ? ob.queued : '—'}</div>
                    <div class="stat-sub">${ob ? ob.applied + ' applied' : 'empty'}</div>
                </div>
            </div>

            <div class="card" style="margin-top:24px">
                <div class="card-title">Run Pipeline Stages</div>
                <p style="color:var(--text-secondary);font-size:14px;margin:8px 0 16px">Trigger a stage — it runs in the background. Poll status below.</p>
                <div style="display:flex;gap:12px;flex-wrap:wrap">
                    <button class="btn" onclick="runStage('ingest', this)">Ingest</button>
                    <button class="btn" onclick="runStage('scrape', this)">Scrape</button>
                    <button class="btn" onclick="runStage('generate', this)">Generate</button>
                </div>
                <div id="run-status" style="margin-top:16px;font-size:14px;color:var(--text-secondary)"></div>
            </div>

            ${au ? `
            <div class="card" style="margin-top:24px">
                <div class="card-title">CV Audit Summary</div>
                <div style="display:flex;gap:16px;margin-top:12px">
                    <span class="badge badge-high">${au.high} high</span>
                    <span class="badge badge-medium">${au.medium} medium</span>
                    <span class="badge badge-low">${au.low} low</span>
                    <span style="color:var(--text-secondary);font-size:14px">${au.strengths} strengths · ${au.weaknesses} weaknesses</span>
                </div>
            </div>` : ''}
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

// --- Profile ---

async function renderProfile() {
    navHighlight('profile');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const [p, proj] = await Promise.all([
            fetchJSON('/api/profile'),
            fetchJSON('/api/profile/projects'),
        ]);
        const initials = (p.contact.name || '?').split(' ').map(w => w[0]).slice(0,2).join('').toUpperCase();
        content().innerHTML = `
            <h1 class="section-title">Candidate Profile</h1>
            <div class="profile-header">
                <div class="profile-avatar">${initials}</div>
                <div>
                    <div class="profile-name">${esc(p.contact.name)}</div>
                    <div class="profile-headline">${esc(p.headline)}</div>
                    <div style="font-size:13px;color:var(--text-secondary);margin-top:4px">
                        ${esc(p.contact.email)} · ${esc(p.contact.location)}
                        ${p.contact.linkedin ? ` · <a href="${esc(p.contact.linkedin)}" target="_blank">LinkedIn</a>` : ''}
                        ${p.contact.github ? ` · <a href="${esc(p.contact.github)}" target="_blank">GitHub</a>` : ''}
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Summary</div>
                <div class="md-content" style="margin-top:8px">${md(p.summary)}</div>
            </div>

            ${p.highlights?.length ? `
            <div class="card">
                <div class="card-title">Highlights</div>
                <ul style="margin:8px 0 0 20px">${p.highlights.map(h => `<li>${esc(h)}</li>`).join('')}</ul>
            </div>` : ''}

            <div class="card">
                <div class="card-title">Skills</div>
                <div style="margin-top:12px">
                ${p.skills?.map(g => `<div class="skill-group"><div class="skill-group-label">${esc(g.label)}</div><div class="skill-chips">${g.items.map(i => `<span class="chip">${esc(i)}</span>`).join('')}</div></div>`).join('')}
                </div>
            </div>

            <div class="card">
                <div class="card-title">Experience</div>
                <div style="margin-top:12px">
                ${p.roles?.map(r => `
                    <div class="timeline-item">
                        <div class="timeline-title">${esc(r.title)}</div>
                        <div class="timeline-org">${esc(r.org)} · ${esc(r.location)}</div>
                        <div class="timeline-period">${esc(r.start)} → ${r.end ? esc(r.end) : 'present'}</div>
                        ${r.bullets?.length ? `<ul class="timeline-bullets">${r.bullets.map(b => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
                    </div>`).join('')}
                </div>
            </div>

            ${proj.projects?.length ? `
            <div class="card">
                <div class="card-title">Projects</div>
                <div style="margin-top:12px">
                ${proj.projects.map(pr => `
                    <div class="timeline-item">
                        <div class="timeline-title">${esc(pr.title)}</div>
                        ${pr.one_liner ? `<div class="timeline-org">${esc(pr.one_liner)}</div>` : ''}
                    </div>`).join('')}
                </div>
            </div>` : ''}
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

// --- Jobs ---

let jobFilters = { offset: 0, limit: 50, source: '', remote: '', min_pay: '', search: '' };

async function renderJobs() {
    navHighlight('jobs');
    content().innerHTML = `
        <h1 class="section-title">Jobs</h1>
        <div class="filters">
            <input type="text" id="job-search" placeholder="Search company or title…" value="${esc(jobFilters.search)}" oninput="debounceSearch()">
            <select id="job-source" onchange="jobFilterChange()">
                <option value="">All sources</option>
            </select>
            <select id="job-remote" onchange="jobFilterChange()">
                <option value="">All remote</option>
            </select>
            <input type="number" id="job-minpay" placeholder="Min pay" style="width:90px" value="${esc(jobFilters.min_pay)}" onchange="jobFilterChange()">
        </div>
        <div id="job-table"><div class="loading">Loading…</div></div>
    `;
    loadJobs();
}

async function loadJobs() {
    try {
        const params = new URLSearchParams({ offset: jobFilters.offset, limit: jobFilters.limit });
        if (jobFilters.source) params.set('source', jobFilters.source);
        if (jobFilters.remote) params.set('remote', jobFilters.remote);
        if (jobFilters.min_pay) params.set('min_pay', jobFilters.min_pay);
        if (jobFilters.search) params.set('search', jobFilters.search);
        const data = await fetchJSON(`/api/jobs?${params}`);

        // Populate filter dropdowns once
        const srcSel = $('job-source');
        if (srcSel.children.length <= 1) {
            const sources = [...new Set(data.jobs.map(j => j.source))];
            sources.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; srcSel.appendChild(o); });
            if (jobFilters.source) srcSel.value = jobFilters.source;
        }
        const remSel = $('job-remote');
        if (remSel.children.length <= 1) {
            const remotes = [...new Set(data.jobs.map(j => j.remote_scope))];
            remotes.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; remSel.appendChild(o); });
            if (jobFilters.remote) remSel.value = jobFilters.remote;
        }

        $('job-table').innerHTML = `
            <div class="card" style="padding:0;overflow:hidden">
                <table>
                    <thead><tr>
                        <th>Company</th><th>Title</th><th>Pay</th><th>Salary</th><th>Remote</th><th>Source</th>
                    </tr></thead>
                    <tbody>
                        ${data.jobs.map(j => `<tr>
                            <td><strong>${esc(j.company)}</strong></td>
                            <td>${esc(j.title)}</td>
                            <td><span class="score-badge ${scoreClass(j.pay_score)}" style="width:36px;height:36px;font-size:14px">${j.pay_score}</span></td>
                            <td>${esc(fmtSalary(j))}</td>
                            <td><span class="badge badge-low">${esc(j.remote_scope)}</span></td>
                            <td style="color:var(--text-secondary)">${esc(j.source)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>
            <div class="pagination">
                <button class="btn btn-sm btn-secondary" ${jobFilters.offset === 0 ? 'disabled' : ''} onclick="jobPrev()">Prev</button>
                <span style="color:var(--text-secondary);font-size:14px">${jobFilters.offset + 1}–${jobFilters.offset + data.jobs.length} of ${data.total}</span>
                <button class="btn btn-sm btn-secondary" ${jobFilters.offset + jobFilters.limit >= data.total ? 'disabled' : ''} onclick="jobNext()">Next</button>
            </div>
        `;
    } catch (e) { $('job-table').innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

function jobFilterChange() {
    jobFilters.source = $('job-source').value;
    jobFilters.remote = $('job-remote').value;
    jobFilters.min_pay = $('job-minpay').value;
    jobFilters.offset = 0;
    loadJobs();
}

let _searchTimer;
function debounceSearch() {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => {
        jobFilters.search = $('job-search').value;
        jobFilters.offset = 0;
        loadJobs();
    }, 300);
}

function jobPrev() { jobFilters.offset = Math.max(0, jobFilters.offset - jobFilters.limit); loadJobs(); }
function jobNext() { jobFilters.offset += jobFilters.limit; loadJobs(); }

// --- Packages ---

async function renderPackages() {
    navHighlight('packages');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const data = await fetchJSON('/api/packages');
        if (!data.packages.length) {
            content().innerHTML = `<h1 class="section-title">Packages</h1><div class="card"><p style="color:var(--text-secondary)">No packages yet. Run <code>jobapp generate</code> to build some.</p></div>`;
            return;
        }
        content().innerHTML = `
            <h1 class="section-title">Packages (${data.total})</h1>
            <div class="grid grid-2">
                ${data.packages.map(p => `
                    <div class="pkg-card" onclick="location.hash='#/packages/${esc(p.slug)}'">
                        <div class="pkg-header">
                            <span class="score-badge ${scoreClass(p.match.score)}">${p.match.score}</span>
                            <div>
                                <div class="pkg-company">${esc(p.job.company)}</div>
                                <div class="pkg-title">${esc(p.job.title)}</div>
                            </div>
                        </div>
                        <div class="pkg-meta">
                            <span>${p.resume_pages || '?'}p resume</span>
                            <span>${esc(p.job.remote_scope)}</span>
                            <span>score ${p.match.score}</span>
                            ${p.job.salary_usd_estimate ? `<span>~$${p.job.salary_usd_estimate.toLocaleString()}</span>` : ''}
                        </div>
                    </div>`).join('')}
            </div>
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

// --- Package Detail ---

async function renderPackageDetail(slug) {
    navHighlight('packages');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const p = await fetchJSON(`/api/packages/${slug}`);
        const m = p.match;
        const j = p.job;
        const a = p.artifact_texts || {};
        content().innerHTML = `
            <h1 class="section-title">${esc(j.company)} — ${esc(j.title)}</h1>
            <div class="card">
                <div class="card-header">
                    <div style="display:flex;align-items:center;gap:14px">
                        <span class="score-badge ${scoreClass(m.score)}">${m.score}</span>
                        <div>
                            <div style="font-weight:600">Match Score: ${m.score}/100</div>
                            <div style="font-size:13px;color:var(--text-secondary)">${esc(m.persona)}</div>
                        </div>
                    </div>
                    <a href="${esc(j.apply_url)}" target="_blank" class="btn btn-sm">Apply ↗</a>
                </div>
                <div class="md-content" style="font-size:14px;color:var(--text-secondary);margin-top:8px">${esc(m.rationale)}</div>
                ${m.strengths?.length ? `<p style="margin-top:12px"><strong>Strengths:</strong> ${m.strengths.map(s => esc(s)).join('; ')}</p>` : ''}
                ${m.gaps?.length ? `<p><strong>Gaps:</strong> ${m.gaps.map(s => esc(s)).join('; ')}</p>` : ''}
            </div>

            <div class="tabs">
                <button class="tab active" onclick="switchTab(event,'resume')">Resume</button>
                <button class="tab" onclick="switchTab(event,'cover')">Cover Letter</button>
                <button class="tab" onclick="switchTab(event,'post')">Post</button>
                <button class="tab" onclick="switchTab(event,'study')">Study Plan</button>
                <button class="tab" onclick="switchTab(event,'answers')">Answers</button>
                <button class="tab" onclick="switchTab(event,'job')">Job Posting</button>
            </div>

            <div id="tab-resume" class="tab-panel">
                <iframe class="resume-frame" src="/api/packages/${esc(slug)}/resume.pdf"></iframe>
            </div>
            <div id="tab-cover" class="tab-panel" style="display:none">
                <div class="card"><div class="md-content">${md(a['cover-letter.md'])}</div></div>
            </div>
            <div id="tab-post" class="tab-panel" style="display:none">
                <div class="card"><div class="md-content">${md(a['post.md'])}</div></div>
            </div>
            <div id="tab-study" class="tab-panel" style="display:none">
                <div class="card"><div class="md-content">${md(a['study-plan.md'])}</div></div>
            </div>
            <div id="tab-answers" class="tab-panel" style="display:none">
                <div class="card"><div class="md-content">${md(a['answers.md'])}</div></div>
            </div>
            <div id="tab-job" class="tab-panel" style="display:none">
                <div class="card"><div class="md-content">${md(a['job.md'])}</div></div>
            </div>
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

function switchTab(e, name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    e.target.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
    $(`tab-${name}`).style.display = '';
}

// --- Outbound ---

async function renderOutbound() {
    navHighlight('outbound');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const [data, sent] = await Promise.all([
            fetchJSON('/api/outbound'),
            fetchJSON('/api/outbound/sent'),
        ]);
        if (!data.rows.length) {
            content().innerHTML = `<h1 class="section-title">Outbound</h1><div class="card"><p style="color:var(--text-secondary)">Queue is empty. Run <code>jobapp generate</code> to populate it.</p></div>`;
            return;
        }
        content().innerHTML = `
            <h1 class="section-title">Outbound Queue (${data.rows.length})</h1>
            <div class="card" style="padding:0;overflow:hidden">
                <table>
                    <thead><tr>
                        <th>Company</th><th>Role</th><th>Match</th><th>Pay</th><th>Salary</th><th>Remote</th><th>Applied</th><th>Action</th>
                    </tr></thead>
                    <tbody>
                        ${data.rows.map(r => {
                            const slug = (r.package_dir || '').replace('output/', '');
                            const applied = r.applied_on
                                ? `<span class="badge badge-low">${esc(r.applied_on)}</span>`
                                : '—';
                            return `<tr>
                                <td><strong>${esc(r.company)}</strong></td>
                                <td>${esc(r.role)}</td>
                                <td><span class="score-badge ${scoreClass(parseInt(r.match_score)||0)}" style="width:32px;height:32px;font-size:13px">${esc(r.match_score)}</span></td>
                                <td>${esc(r.pay_score)}</td>
                                <td>${r.salary_usd_est ? '$' + esc(r.salary_usd_est) : '—'}</td>
                                <td>${esc(r.remote_scope)}</td>
                                <td>${applied}</td>
                                <td><button class="btn btn-sm" onclick="renderApplyPanel('${esc(slug)}')">Apply</button></td>
                            </tr>`;
                        }).join('')}
                    </tbody>
                </table>
            </div>
            ${sent.entries?.length ? `
            <h2 style="font-size:20px;font-weight:700;margin:32px 0 16px;letter-spacing:-0.3px">Sent Applications (${sent.entries.length})</h2>
            <div class="card" style="padding:0;overflow:hidden">
                <table>
                    <thead><tr><th>Company</th><th>Role</th><th>Method</th><th>Result</th><th>Date</th></tr></thead>
                    <tbody>
                        ${sent.entries.map(e => `<tr>
                            <td><strong>${esc(e.company)}</strong></td>
                            <td>${esc(e.role)}</td>
                            <td>${esc(e.method)}</td>
                            <td><span class="badge ${e.result === 'submitted' ? 'badge-low' : 'badge-medium'}">${esc(e.result)}</span></td>
                            <td>${esc((e.applied_at||'').slice(0,10))}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>` : ''}
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

// --- Apply panel (human-in-the-loop) ---

async function renderApplyPanel(slug) {
    navHighlight('outbound');
    content().innerHTML = '<div class="loading">Preparing application…</div>';
    try {
        const resp = await fetch(`/api/outbound/${slug}/prepare`, { method: 'POST' });
        if (!resp.ok) throw new Error(`Prepare failed: ${resp.status}`);
        const app = await resp.json();
        const fields = app.fields || [];
        const review = app.needs_review;

        content().innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
                <h1 class="section-title" style="margin:0">Apply: ${esc(app.company)}</h1>
                <a href="#/outbound" class="btn btn-sm btn-secondary">← Back</a>
            </div>
            <div class="card">
                <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
                    <div>
                        <div style="font-weight:600;font-size:17px">${esc(app.role)}</div>
                        <div style="color:var(--text-secondary);font-size:14px;margin-top:2px">
                            Board: ${esc(app.board)} ·
                            ${review
                                ? '<span style="color:var(--orange)">⚠ Fields need your review</span>'
                                : '<span style="color:var(--green)">✓ Ready to submit</span>'}
                        </div>
                    </div>
                    <div style="display:flex;gap:8px;margin-left:auto">
                        <a href="${esc(app.apply_url)}" target="_blank" class="btn btn-sm">Open Page ↗</a>
                        <a href="${esc(app.resume_url)}" target="_blank" class="btn btn-sm btn-secondary">Resume</a>
                        <a href="${esc(app.cover_letter_url)}" target="_blank" class="btn btn-sm btn-secondary">Cover Letter</a>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-top:16px">
                <div class="card-title">Application Fields</div>
                <p style="color:var(--text-secondary);font-size:14px;margin:4px 0 16px">
                    ${review
                        ? 'Fields in red need your input. Edit any field, then open the application page and submit.'
                        : 'All fields pre-filled. Review, then open the application page and submit.'}
                </p>
                <div id="apply-fields">
                ${fields.map((f, i) => `
                    <div class="apply-field ${f.needs_input ? 'needs-input' : ''}" style="margin-bottom:16px">
                        <label style="font-weight:600;font-size:14px;display:block;margin-bottom:6px">
                            ${esc(f.question)}
                            ${f.needs_input ? '<span class="badge badge-high" style="margin-left:8px">NEEDS INPUT</span>' : ''}
                            ${f.type === 'file' ? '<span class="badge badge-low" style="margin-left:8px">FILE</span>' : ''}
                        </label>
                        ${f.type === 'select' && f.options?.length ? `
                            <select class="apply-input" data-field="${i}" style="width:100%">
                                ${f.options.map(o => `<option value="${esc(o)}" ${o === f.answer ? 'selected' : ''}>${esc(o)}</option>`).join('')}
                            </select>
                        ` : f.type === 'file' ? `
                            <div style="padding:10px 14px;background:var(--bg);border-radius:var(--radius-sm);color:var(--text-secondary);font-size:14px">
                                Upload manually — use the Resume / Cover Letter links above.
                            </div>
                        ` : `
                            <textarea class="apply-input" data-field="${i}" rows="${(f.answer||'').length > 200 ? 6 : 3}"
                                style="width:100%;font-family:inherit;font-size:14px;resize:vertical;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg-elevated);color:var(--text);padding:8px 12px"
                            >${esc(f.answer)}</textarea>
                        `}
                    </div>
                `).join('')}
                </div>
            </div>

            <div style="display:flex;gap:12px;margin-top:20px;flex-wrap:wrap">
                <a href="${esc(app.apply_url)}" target="_blank" class="btn">Open Application Page ↗</a>
                <button class="btn" onclick="submitApplication('${esc(slug)}')">Mark as Applied</button>
                <a href="#/outbound" class="btn btn-secondary">Cancel</a>
            </div>
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

async function submitApplication(slug) {
    const fields = {};
    document.querySelectorAll('.apply-input').forEach(el => {
        fields[el.dataset.field] = el.value;
    });
    try {
        const resp = await fetch(`/api/outbound/${slug}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ method: 'manual', result: 'submitted', fields }),
        });
        if (!resp.ok) throw new Error(`${resp.status}`);
        location.hash = '#/outbound';
    } catch (e) {
        alert('Submit failed: ' + e.message);
    }
}

// --- Audit ---

async function renderAudit() {
    navHighlight('audit');
    content().innerHTML = '<div class="loading">Loading…</div>';
    try {
        const audit = await fetchJSON('/api/audit');
        const flags = audit.flags || [];
        const sevColor = { high: 'badge-high', medium: 'badge-medium', low: 'badge-low' };
        content().innerHTML = `
            <h1 class="section-title">CV Audit</h1>
            <div class="grid grid-3">
                <div class="stat"><div class="stat-label">Total Flags</div><div class="stat-value">${flags.length}</div></div>
                <div class="stat"><div class="stat-label">Strengths</div><div class="stat-value" style="color:var(--green)">${(audit.strengths||[]).length}</div></div>
                <div class="stat"><div class="stat-label">Weaknesses</div><div class="stat-value" style="color:var(--orange)">${(audit.weaknesses||[]).length}</div></div>
            </div>

            ${flags.length ? `
            <div class="card" style="margin-top:24px">
                <div class="card-title">Flags</div>
                <div style="margin-top:12px">
                ${flags.map(f => `
                    <div style="border-bottom:1px solid var(--border);padding:12px 0">
                        <span class="badge ${sevColor[f.severity]||'badge-low'}">${esc(f.severity)}</span>
                        <span style="font-weight:600;margin-left:8px">${esc(f.kind)}</span>
                        <p style="margin:6px 0 0;font-size:14px;color:var(--text-secondary)">${esc(f.detail)}</p>
                    </div>`).join('')}
                </div>
            </div>` : ''}

            ${(audit.strengths||[]).length ? `
            <div class="card">
                <div class="card-title" style="color:var(--green)">Strengths</div>
                <ul style="margin:8px 0 0 20px">${audit.strengths.map(s => `<li>${esc(s)}</li>`).join('')}</ul>
            </div>` : ''}

            ${(audit.weaknesses||[]).length ? `
            <div class="card">
                <div class="card-title" style="color:var(--orange)">Weaknesses</div>
                <ul style="margin:8px 0 0 20px">${audit.weaknesses.map(s => `<li>${esc(s)}</li>`).join('')}</ul>
            </div>` : ''}
        `;
    } catch (e) { content().innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

// --- Run pipeline stages ---

async function runStage(stage, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }
    const statusEl = $('run-status');
    if (statusEl) statusEl.textContent = `${stage} started…`;

    try {
        const resp = await fetch(`/api/run/${stage}`, { method: 'POST' });
        const data = await resp.json();
        if (statusEl) statusEl.textContent = `${stage}: ${data.status}`;
        // Poll
        const poll = setInterval(async () => {
            try {
                const s = await fetchJSON(`/api/run/${stage}/status`);
                if (s.status !== 'running') {
                    clearInterval(poll);
                    if (btn) { btn.disabled = false; btn.textContent = stage.charAt(0).toUpperCase() + stage.slice(1); }
                    if (statusEl) statusEl.textContent = `${stage}: ${s.status}`;
                    renderDashboard();
                }
            } catch {}
        }, 3000);
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = stage.charAt(0).toUpperCase() + stage.slice(1); }
        if (statusEl) statusEl.textContent = `Error: ${e.message}`;
    }
}

// --- Router ---

function router() {
    const hash = location.hash.slice(1) || '/';
    const parts = hash.split('/').filter(Boolean);

    if (parts.length === 0) renderDashboard();
    else if (parts[0] === 'profile') renderProfile();
    else if (parts[0] === 'jobs') renderJobs();
    else if (parts[0] === 'packages') parts.length > 1 ? renderPackageDetail(parts[1]) : renderPackages();
    else if (parts[0] === 'outbound') parts.length > 2 && parts[1] === 'apply' ? renderApplyPanel(parts[2]) : renderOutbound();
    else if (parts[0] === 'audit') renderAudit();
    else renderDashboard();
}

window.addEventListener('hashchange', router);
window.addEventListener('load', router);
