# Elder Tech Jobs

Dashboard tracking **data / IT / AI job openings at elder-support organizations across the US** — Area Agencies on Aging, national aging nonprofits (AARP, Alzheimer's Association, NCOA), AgeTech companies (Honor, WellSky, PointClickCare…), and senior-focused healthcare (PACE programs, VNS Health, ChenMed, Devoted Health…).

**Live dashboard:** https://iknalos.github.io/ElderTechJobs/

## How it works

- `scraper.py` queries each employer's job-board API directly (Greenhouse, Lever, Ashby, Workday CXS, Jibe), keeps only data/IT/AI/software titles, and writes `docs/jobs.json`.
- `manual_jobs.json` holds curated postings from agencies whose career sites have no public API (NYC Aging, Council on Aging SW Ohio, Element Care PACE, etc.). Edit this file to add/remove leads.
- A GitHub Actions workflow (`.github/workflows/update.yml`) runs the scraper **every day at 11:00 UTC** and commits the refreshed `jobs.json`; GitHub Pages serves `docs/` as the dashboard.

## Run locally

```
pip install requests
python scraper.py
start docs\index.html   # then serve docs/ with any static server, e.g. python -m http.server -d docs
```

## Adding a new company

If it uses a standard ATS, add one line to `SOURCES` in `scraper.py`:

- Greenhouse: `("Name", fetch_greenhouse, ("board-slug",))`
- Lever: `("Name", fetch_lever, ("org-slug",))`
- Ashby: `("Name", fetch_ashby, ("org-slug",))`
- Jibe-style (`/api/jobs`): `("Name", fetch_jibe, ("https://careers.example.com",))`
- Workday: `("Name", fetch_workday, ("host", "tenant", "site"))`

Otherwise add the posting to `manual_jobs.json`.
