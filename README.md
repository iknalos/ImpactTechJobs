# ImpactTech Jobs

Dashboard tracking **data / software / AI / IT job openings at mission-driven organizations** — healthcare & academic medicine (Mass General Brigham, Dana-Farber, VNS Health, PACE programs…), universities & research orgs (US and European), national nonprofits (Red Cross, ACS, AARP, ACLU…), civic tech (Code for America, Nava), aging services & AgeTech (WellSky, PointClickCare, Honor…), plus a curated **Abroad** section of relocation-feasible European employers.

**Live dashboard:** https://iknalos.github.io/ImpactTechJobs/

## How it works

- `scraper.py` queries each employer's job-board API directly (Greenhouse, Lever, Ashby, Workday CXS, Jibe, SmartRecruiters, Varbi, Pinpoint…), keeps only data/IT/AI/software titles, and writes `docs/jobs.json`.
- `manual_jobs.json` holds curated postings from agencies whose career sites have no public API (NYC Aging, Council on Aging SW Ohio, Element Care PACE, etc.). Edit this file to add/remove leads.
- A GitHub Actions workflow (`.github/workflows/update.yml`) runs the scraper **every day at 11:00 UTC** and commits the refreshed `jobs.json`; GitHub Pages serves `docs/` as the dashboard.

## Run locally

```
pip install requests
python scraper.py
start docs\index.html   # then serve docs/ with any static server, e.g. python -m http.server -d docs
```

## One-click resume generation (local bridge)

`resume.html` tailors a resume per job. For one-click generation without an API key, run the local bridge:

```
start_resume_bridge.bat
```

It serves `127.0.0.1:8765` and pipes prompts through the local Claude Code CLI (`claude -p --tools ""` — text-only, no tool access). The site auto-detects it; when it's not running, the page falls back to an Anthropic API key (⚙️ settings) or a manual copy-prompt flow.

## Adding a new company

If it uses a standard ATS, add one line to `SOURCES` in `scraper.py`:

- Greenhouse: `("Name", fetch_greenhouse, ("board-slug",))`
- Lever: `("Name", fetch_lever, ("org-slug",))`
- Ashby: `("Name", fetch_ashby, ("org-slug",))`
- Jibe-style (`/api/jobs`): `("Name", fetch_jibe, ("https://careers.example.com",))`
- Workday: `("Name", fetch_workday, ("host", "tenant", "site"))`

Otherwise add the posting to `manual_jobs.json`.
