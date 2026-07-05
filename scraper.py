"""Elder Tech Jobs scraper.

Pulls current job postings from the ATS APIs of elder-support / aging-services
organizations, keeps only data / IT / AI / software roles, merges in manually
curated postings (manual_jobs.json), and writes docs/jobs.json for the
dashboard (docs/index.html).

Run: python scraper.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "jobs.json"
MANUAL = ROOT / "manual_jobs.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (ElderTechJobs job dashboard; personal use)"}
TIMEOUT = 30

# ---------------------------------------------------------------- filtering

INCLUDE = re.compile(
    r"\b(data|analyst|analytics|engineer(ing)?|software|developer|dev\s?ops|"
    r"database|informatics?|informaticist|scientist|business intelligence|"
    r"machine learning|artificial intelligence|cloud|platform|digital|"
    r"technology|technical|systems?|prompt|salesforce|tableau|power\s?bi|"
    r"AI|ML|BI|IT|IAM|SRE|CRM)\b",
    re.IGNORECASE,
)
EXCLUDE = re.compile(
    r"\b(nurse|RN|LPN|CNA|aide|driver|cook|chef|dietary|housekeep\w*|chaplain|"
    r"therapist|caregiver|care giver|social worker|custodian|janitor|"
    r"receptionist|phlebotom\w*|medical assistant)\b",
    re.IGNORECASE,
)

AI_RE = re.compile(r"\b(AI|ML|machine learning|artificial intelligence|prompt|GenAI|LLM)\b", re.I)
DATA_RE = re.compile(r"\b(data|analyst|analytics|scientist|database|business intelligence|informatic\w*|BI|tableau|power\s?bi)\b", re.I)
SW_RE = re.compile(r"\b(software|developer|engineer(ing)?|dev\s?ops|SRE|platform|cloud|full.?stack|backend|frontend)\b", re.I)


def wanted(title):
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


def categorize(title):
    if AI_RE.search(title):
        return "AI"
    if DATA_RE.search(title):
        return "Data"
    if SW_RE.search(title):
        return "Software"
    return "IT"


def job(org, title, location, url, posted_iso, source):
    return {
        "org": org,
        "title": title.strip(),
        "location": (location or "").strip() or "See posting",
        "url": url,
        "posted": posted_iso,  # ISO date string or None
        "category": categorize(title),
        "source": source,
    }


# ---------------------------------------------------------------- fetchers

def fetch_greenhouse(org, board):
    r = requests.get(
        "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=false" % board,
        headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        if not wanted(title):
            continue
        posted = (j.get("first_published") or j.get("updated_at") or "")[:10] or None
        out.append(job(org, title, (j.get("location") or {}).get("name"),
                       j.get("absolute_url"), posted, "greenhouse"))
    return out


def fetch_lever(org, slug):
    r = requests.get("https://api.lever.co/v0/postings/%s?mode=json" % slug,
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        title = j.get("text", "")
        if not wanted(title):
            continue
        posted = None
        if j.get("createdAt"):
            posted = datetime.fromtimestamp(j["createdAt"] / 1000, tz=timezone.utc).date().isoformat()
        loc = (j.get("categories") or {}).get("location")
        out.append(job(org, title, loc, j.get("hostedUrl"), posted, "lever"))
    return out


def fetch_ashby(org, slug):
    r = requests.get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug,
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        if not wanted(title):
            continue
        posted = (j.get("publishedAt") or "")[:10] or None
        loc = j.get("location") or ("Remote" if j.get("isRemote") else "")
        out.append(job(org, title, loc, j.get("jobUrl") or j.get("applyUrl"), posted, "ashby"))
    return out


def fetch_jibe(org, base):
    """Jibe/iCIMS-style boards: {base}/api/jobs?page=N"""
    out, page = [], 1
    while page <= 15:
        r = requests.get("%s/api/jobs" % base, params={"page": page},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        if not jobs:
            break
        for wrap in jobs:
            d = wrap.get("data", wrap)
            title = d.get("title", "")
            if not wanted(title):
                continue
            posted = (d.get("posted_date") or d.get("create_date") or "")[:10] or None
            url = (d.get("meta_data") or {}).get("canonical_url") or \
                  "%s/jobs/%s" % (base, d.get("slug") or d.get("req_id", ""))
            loc = d.get("full_location") or d.get("city") or ""
            out.append(job(org, title, loc, url, posted, "jibe"))
        page += 1
    return out


WD_AGO = re.compile(r"(\d+)\+?\s+day", re.I)


def _workday_posted(text):
    if not text:
        return None
    t = text.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = WD_AGO.search(t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return None


WD_TERMS = ["data", "analyst", "engineer", "software", "developer", "analytics",
            "information technology", "AI", "systems", "database", "cloud"]


def fetch_workday(org, host, tenant, site):
    """Workday CXS JSON API.

    Searches per keyword instead of dumping the whole board: large tenants
    have thousands of postings, and some ignore the offset parameter and
    return page 1 forever — the seen-path check below breaks that loop.
    """
    url = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
    seen, out = set(), []
    for term in WD_TERMS:
        offset = 0
        while offset < 200:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            r = requests.post(url, json=body, headers=dict(HEADERS, Accept="application/json"),
                              timeout=TIMEOUT)
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            new = 0
            for j in postings:
                path = j.get("externalPath")
                title = j.get("title", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                new += 1
                if not title or not wanted(title):
                    continue
                jurl = "https://%s/en-US/%s%s" % (host, site, path)
                out.append(job(org, title, j.get("locationsText"), jurl,
                               _workday_posted(j.get("postedOn")), "workday"))
            if new == 0:
                break
            offset += 20
    return out


SOURCES = [
    ("Honor / Home Instead", fetch_greenhouse, ("honor",)),
    ("Wellthy", fetch_greenhouse, ("wellthy",)),
    ("AlayaCare", fetch_greenhouse, ("alayacare",)),
    ("PointClickCare", fetch_lever, ("pointclickcare",)),
    ("HHAeXchange", fetch_lever, ("hhaexchange",)),
    ("August Health", fetch_ashby, ("august-health",)),
    ("AARP", fetch_jibe, ("https://careers.aarp.org",)),
    ("InnovAge (PACE)", fetch_jibe, ("https://careers.innovage.com",)),
    ("VNS Health", fetch_jibe, ("https://jobs.vnshealth.org",)),
    ("WellSky", fetch_workday, ("wellsky.wd1.myworkdayjobs.com", "wellsky", "WellSkyCareers")),
    ("Devoted Health", fetch_workday, ("devoted.wd1.myworkdayjobs.com", "devoted", "Devoted")),
    ("ChenMed", fetch_workday, ("chenmed.wd1.myworkdayjobs.com", "chenmed", "ChenMed")),
    ("Cityblock Health", fetch_workday, ("cityblockhealth.wd1.myworkdayjobs.com", "cityblockhealth", "CityblockExternalCareerSite")),
    ("Sunrise Senior Living", fetch_workday, ("sunriseseniorliving.wd12.myworkdayjobs.com", "sunriseseniorliving", "SUNRISE_EXT_CAREERS")),
]


def main():
    all_jobs, errors = [], []
    for org, fn, args in SOURCES:
        try:
            found = fn(org, *args)
            print("  %-28s %3d roles" % (org, len(found)))
            all_jobs.extend(found)
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            print("  %-28s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})

    if MANUAL.exists():
        manual = json.loads(MANUAL.read_text(encoding="utf-8"))
        for m in manual:
            m.setdefault("source", "manual")
            m.setdefault("category", categorize(m["title"]))
        all_jobs.extend(manual)
        print("  %-28s %3d roles" % ("manual_jobs.json", len(manual)))

    # de-dupe by URL
    seen, unique = set(), []
    for j in all_jobs:
        key = (j.get("url") or j["org"]) + "|" + j["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)

    unique.sort(key=lambda j: j.get("posted") or "0000", reverse=True)

    OUT.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(unique),
        "errors": errors,
        "jobs": unique,
    }, indent=1), encoding="utf-8")
    print("Wrote %d unique jobs -> %s (%d source errors)" % (len(unique), OUT, len(errors)))


if __name__ == "__main__":
    main()
