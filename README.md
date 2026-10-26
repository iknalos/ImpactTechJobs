# ImpactTech Jobs

Dashboard tracking **data / software / AI / IT job openings at mission-driven organizations** — healthcare & academic medicine (Mass General Brigham, Dana-Farber, VNS Health, PACE programs…), universities & research orgs (US and European), national nonprofits (Red Cross, ACS, AARP, ACLU…), civic tech (Code for America, Nava), aging services & AgeTech (WellSky, PointClickCare, Honor…), plus a curated **Abroad** section of relocation-feasible European employers.

**Live dashboard:** https://iknalos.github.io/ImpactTechJobs/

## How it works

- `scraper.py` queries each employer's job-board API directly (Greenhouse, Lever, Ashby, Workday CXS, Jibe, SmartRecruiters, Varbi, Pinpoint…), keeps only data/IT/AI/software titles, and writes `docs/jobs.json`.
- `manual_jobs.json` holds curated postings from employers whose career sites have no public API. Every run re-checks each one and skips postings whose page is gone (404/410, "no longer accepting applications", or the title no longer on the page), so stale leads drop off by themselves. Prefer an automated source over a manual entry whenever the ATS has a feed.
- A GitHub Actions workflow (`.github/workflows/update.yml`) runs the scraper **every day at 07:41 UTC** (GitHub often starts scheduled runs a few hours late; the odd minute and early hour keep it before the workday) and commits the refreshed `jobs.json`; GitHub Pages serves `docs/` as the dashboard.

## Source health

One broken source never fails the run, so breakage has to be tracked instead. `jobs.json` carries a `health` entry per source with day streaks carried across runs:

- `fail_days`: consecutive runs where the fetch raised an error
- `empty_days`: consecutive runs where the board returned **no postings at all** (a moved or renamed board usually looks like this, not like an error)
- `zero_days`: days with no *matching* role (normal for small agencies; informational only)

A source is flagged after 3 failing or 3 empty days. Flags show up as warnings on the Actions run page, in the run's job summary table, and on the dashboard ("N source(s) need a look", hover for names).

## H-1B cap-exempt tags

USCIS publishes no list of cap-exempt employers, so the tags are derived from DOL data. Each prevailing-wage request (Form ETA-9141) states whether the employer is covered by ACWIA as an institution of higher education, a university-affiliated nonprofit, or a nonprofit/governmental research organization: the same categories that make an employer cap-exempt (INA 214(g)(5)). An employer is tagged only if DOL issued the ACWIA wage to it at least once.

- `data/acwia_employers.json`: ACWIA-covered employers, built by `tools/build_acwia.py`
- `cap_exempt_orgs.json`: maps dashboard org names to their DOL legal names (e.g. Harvard University -> President and Fellows of Harvard College); `system: true` marks brands where only some legal entities are covered (hospital systems)
- The daily run stamps matching US jobs with `cap_exempt: true` automatically.

**Quarterly refresh (manual, ~2 minutes):** dol.gov blocks scripted downloads, so download the newest `PW_Disclosure_Data_FY20xx_Qx.xlsx` files from https://www.dol.gov/agencies/eta/foreign-labor/performance in a browser, then

```
pip install openpyxl
python tools/build_acwia.py %USERPROFILE%\Downloads\PW_Disclosure_Data_FY2026_Q3.xlsx %USERPROFILE%\Downloads\PW_Disclosure_Data_FY2025_Q4.xlsx
```

and commit `data/acwia_employers.json`. The workflow warns when the newest file's period ended more than 270 days ago.

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
- ADP Workforce Now (`recruitment.html?cid=...`): `("Name", fetch_adp, ("cid",))`

Otherwise add the posting to `manual_jobs.json`.
