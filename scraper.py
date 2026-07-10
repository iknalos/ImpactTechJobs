"""Elder Tech Jobs scraper.

Pulls current job postings from the ATS APIs of elder-support / aging-services
organizations, keeps only data / IT / AI / software roles, merges in manually
curated postings (manual_jobs.json), and writes docs/jobs.json for the
dashboard (docs/index.html).

Run: python scraper.py
"""

import html as htmllib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "jobs.json"
DESC_OUT = ROOT / "docs" / "desc.json"
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


# years-of-experience requirement: "5+ years of experience", "experience: 5 years"
YEARS_FWD_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b[^.;]{0,50}?\b(?:experience|exp)\b", re.I)
YEARS_BCK_RE = re.compile(r"\bexperience\b[^.;]{0,30}?(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)


def years_required(desc):
    found = [int(m.group(1)) for m in YEARS_FWD_RE.finditer(desc)]
    found += [int(m.group(1)) for m in YEARS_BCK_RE.finditer(desc)]
    found = [y for y in found if 1 <= y <= 20]
    return max(found) if found else None


# phrases meaning F-1/OPT candidates cannot apply (citizenship / green card /
# clearance requirements). Deliberately does NOT match "no sponsorship" — OPT
# does not need sponsorship.
CITIZEN_RE = re.compile(
    r"(\bu\.?s\.?\s*citizen|united states citizen|citizenship\s+(is\s+)?required|"
    r"must\s+be\s+a\s+citizen|green\s*card|permanent\s+resident|lawful\s+permanent|"
    r"security\s+clearance|\bu\.?s\.?\s+person\b|\bitar\b)",
    re.IGNORECASE,
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s):
    if not s:
        return ""
    s = htmllib.unescape(htmllib.unescape(s))
    return WS_RE.sub(" ", TAG_RE.sub(" ", s)).strip()[:8000]


def job(org, title, location, url, posted_iso, source, desc=""):
    return {
        "org": org,
        "title": title.strip(),
        "location": (location or "").strip() or "See posting",
        "url": url,
        "posted": posted_iso,  # ISO date string or None
        "category": categorize(title),
        "source": source,
        "_desc": desc,  # stripped out into desc.json before writing jobs.json
    }


# ---------------------------------------------------------------- fetchers

def fetch_greenhouse(org, board):
    r = requests.get(
        "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % board,
        headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        if not wanted(title):
            continue
        posted = (j.get("first_published") or j.get("updated_at") or "")[:10] or None
        out.append(job(org, title, (j.get("location") or {}).get("name"),
                       j.get("absolute_url"), posted, "greenhouse",
                       strip_html(j.get("content", ""))))
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
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists", []):
            desc += " " + lst.get("text", "") + ": " + strip_html(lst.get("content", ""))
        desc += " " + (j.get("additionalPlain") or strip_html(j.get("additional", "")))
        out.append(job(org, title, loc, j.get("hostedUrl"), posted, "lever",
                       WS_RE.sub(" ", desc).strip()[:8000]))
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
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
        out.append(job(org, title, loc, j.get("jobUrl") or j.get("applyUrl"), posted, "ashby", desc))
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
            out.append(job(org, title, loc, url, posted, "jibe",
                           strip_html(d.get("description", ""))))
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
            "information technology", "AI", "database"]


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
                               _workday_posted(j.get("postedOn")), "workday", path))
            if new == 0:
                break
            offset += 20
    # second pass: fetch full descriptions only for the jobs that matched
    for j in out:
        path, j["_desc"] = j["_desc"], ""
        try:
            r = requests.get("https://%s/wday/cxs/%s/%s%s" % (host, tenant, site, path),
                             headers=dict(HEADERS, Accept="application/json"), timeout=TIMEOUT)
            r.raise_for_status()
            j["_desc"] = strip_html(
                (r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))
        except Exception:
            pass
    return out


def fetch_pinpoint(org, base):
    r = requests.get(base + "/postings.json", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    items = d.get("data", d) if isinstance(d, dict) else d
    out = []
    for j in items or []:
        title = j.get("title", "")
        if not wanted(title):
            continue
        loc = j.get("location")
        if isinstance(loc, dict):
            loc = loc.get("name") or loc.get("city")
        url = j.get("url") or j.get("careers_url") or base
        posted = (j.get("created_at") or j.get("published_at") or "")[:10] or None
        out.append(job(org, title, loc, url, posted, "pinpoint",
                       strip_html(j.get("description", ""))))
    return out


# (org, fetcher, args, sector)
SOURCES = [
    # --- aging services & senior-care (the original core) ---
    ("Honor / Home Instead", fetch_greenhouse, ("honor",), "Tech / Software"),
    ("Wellthy", fetch_greenhouse, ("wellthy",), "Tech / Software"),
    ("AlayaCare", fetch_greenhouse, ("alayacare",), "Tech / Software"),
    ("PointClickCare", fetch_lever, ("pointclickcare",), "Tech / Software"),
    ("HHAeXchange", fetch_lever, ("hhaexchange",), "Tech / Software"),
    ("August Health", fetch_ashby, ("august-health",), "Tech / Software"),
    ("AARP", fetch_jibe, ("https://careers.aarp.org",), "Aging Services"),
    ("InnovAge (PACE)", fetch_jibe, ("https://careers.innovage.com",), "Aging Services"),
    ("VNS Health", fetch_jibe, ("https://jobs.vnshealth.org",), "Healthcare"),
    ("WellSky", fetch_workday, ("wellsky.wd1.myworkdayjobs.com", "wellsky", "WellSkyCareers"), "Tech / Software"),
    ("Devoted Health", fetch_workday, ("devoted.wd1.myworkdayjobs.com", "devoted", "Devoted"), "Healthcare"),
    ("ChenMed", fetch_workday, ("chenmed.wd1.myworkdayjobs.com", "chenmed", "ChenMed"), "Healthcare"),
    ("Cityblock Health", fetch_workday, ("cityblockhealth.wd1.myworkdayjobs.com", "cityblockhealth", "CityblockExternalCareerSite"), "Healthcare"),
    ("Sunrise Senior Living", fetch_workday, ("sunriseseniorliving.wd12.myworkdayjobs.com", "sunriseseniorliving", "SUNRISE_EXT_CAREERS"), "Aging Services"),
    # --- national nonprofits ---
    ("American Red Cross", fetch_workday, ("americanredcross.wd1.myworkdayjobs.com", "americanredcross", "American_Red_Cross_Careers"), "Nonprofit & Civic"),
    ("American Cancer Society", fetch_workday, ("acs.wd5.myworkdayjobs.com", "acs", "ACSCareers"), "Nonprofit & Civic"),
    ("ALSAC / St. Jude", fetch_workday, ("alsacstjude.wd1.myworkdayjobs.com", "alsacstjude", "careersalsacstjude"), "Nonprofit & Civic"),
    ("Planned Parenthood Federation", fetch_lever, ("ppfa",), "Nonprofit & Civic"),
    ("The Trevor Project", fetch_lever, ("thetrevorproject",), "Nonprofit & Civic"),
    ("ACLU", fetch_greenhouse, ("aclu",), "Nonprofit & Civic"),
    ("Code for America", fetch_greenhouse, ("codeforamerica",), "Nonprofit & Civic"),
    ("Nava PBC", fetch_greenhouse, ("navapbc",), "Nonprofit & Civic"),
    ("ABCD Boston", fetch_pinpoint, ("https://careers.bostonabcd.org",), "Nonprofit & Civic"),
    ("Year Up United", fetch_workday, ("yearup.wd503.myworkdayjobs.com", "yearup", "YearUp"), "Nonprofit & Civic"),
    # --- research orgs & academia ---
    ("American Institutes for Research", fetch_greenhouse, ("americaninstitutesforresearch",), "Academia & Research"),
    ("ICF", fetch_workday, ("icf.wd5.myworkdayjobs.com", "icf", "ICFExternal_Career_Site"), "Academia & Research"),
    ("Urban Institute", fetch_workday, ("urban.wd115.myworkdayjobs.com", "urban", "Urban-Careers"), "Academia & Research"),
    ("RAND", fetch_workday, ("rand.wd5.myworkdayjobs.com", "rand", "External_Career_Site"), "Academia & Research"),
    ("Northeastern University", fetch_workday, ("northeastern.wd1.myworkdayjobs.com", "northeastern", "careers"), "Academia & Research"),
    ("Brandeis University", fetch_workday, ("brandeis.wd5.myworkdayjobs.com", "brandeis", "Jobs"), "Academia & Research"),
    ("Tufts University", fetch_jibe, ("https://jobs.tufts.edu",), "Academia & Research"),
    ("UMass Chan Medical School", fetch_jibe, ("https://talent.umassmed.edu",), "Academia & Research"),
    # --- healthcare & academic medicine ---
    ("Mass General Brigham", fetch_workday, ("massgeneralbrigham.wd1.myworkdayjobs.com", "massgeneralbrigham", "MGBExternal"), "Healthcare"),
    ("Beth Israel Lahey Health", fetch_workday, ("bilh.wd1.myworkdayjobs.com", "bilh", "External"), "Healthcare"),
    ("Dana-Farber Cancer Institute", fetch_workday, ("danafarber.wd5.myworkdayjobs.com", "danafarber", "dana-farber"), "Healthcare"),
    ("Boston Medical Center", fetch_workday, ("bmc.wd1.myworkdayjobs.com", "bmc", "BMC"), "Healthcare"),
    ("Tufts Medicine", fetch_workday, ("tuftsmedicine.wd1.myworkdayjobs.com", "tuftsmedicine", "Jobs"), "Healthcare"),
    ("NeighborHealth (EBNHC)", fetch_workday, ("ebnhc.wd1.myworkdayjobs.com", "ebnhc", "EBNHC"), "Healthcare"),
    ("Blue Cross Blue Shield of MA", fetch_workday, ("bcbsma.wd5.myworkdayjobs.com", "bcbsma", "BCBSMA"), "Healthcare"),
    # --- nonprofit-serving software ---
    ("Blackbaud", fetch_workday, ("blackbaud.wd1.myworkdayjobs.com", "blackbaud", "ExternalCareers"), "Tech / Software"),
    ("Bonterra", fetch_workday, ("bonterra.wd1.myworkdayjobs.com", "bonterra", "bonterratech"), "Tech / Software"),
]


def main():
    all_jobs, errors = [], []
    for org, fn, args, sector in SOURCES:
        try:
            found = fn(org, *args)
            for j in found:
                j["sector"] = sector
            print("  %-34s %3d roles" % (org, len(found)))
            all_jobs.extend(found)
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            print("  %-34s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})

    if MANUAL.exists():
        manual = json.loads(MANUAL.read_text(encoding="utf-8"))
        for m in manual:
            m.setdefault("source", "manual")
            m.setdefault("category", categorize(m["title"]))
            m.setdefault("sector", "Aging Services")
        all_jobs.extend(manual)
        print("  %-34s %3d roles" % ("manual_jobs.json", len(manual)))

    # de-dupe by URL; pull descriptions out into their own file and use them
    # to flag citizenship/green-card requirements (F-1/OPT filter)
    seen, unique, descs = set(), [], {}
    for j in all_jobs:
        key = (j.get("url") or j["org"]) + "|" + j["title"]
        if key in seen:
            continue
        seen.add(key)
        desc = j.pop("_desc", "") or ""
        if desc:
            descs[key] = desc
            j["citizen_req"] = bool(CITIZEN_RE.search(desc))
            j["years_req"] = years_required(desc)
        else:
            j["citizen_req"] = None  # unknown (no description available)
            j["years_req"] = j.get("years_req")
        unique.append(j)

    unique.sort(key=lambda j: j.get("posted") or "0000", reverse=True)

    # carry over first_seen from the previous run so the dashboard can show
    # "added today"; jobs from before tracking started keep first_seen=null
    prev_seen, had_prev = {}, False
    if OUT.exists():
        try:
            for old in json.loads(OUT.read_text(encoding="utf-8")).get("jobs", []):
                k = (old.get("url") or old.get("org", "")) + "|" + old.get("title", "")
                prev_seen[k] = old.get("first_seen")
            had_prev = True
        except Exception:
            pass
    today = datetime.now(timezone.utc).date().isoformat()
    new_today = 0
    for j in unique:
        k = (j.get("url") or j["org"]) + "|" + j["title"]
        if k in prev_seen:
            j["first_seen"] = prev_seen[k]
        else:
            # on the very first tracked run everything would count as "new";
            # suppress that by only stamping dates once a previous file exists
            j["first_seen"] = today if had_prev else None
        if j["first_seen"] == today:
            new_today += 1

    payload = {
        "new_today": new_today,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(unique),
        "errors": errors,
        "jobs": unique,
    }
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    DESC_OUT.write_text(json.dumps(descs), encoding="utf-8")
    # .js mirrors let the pages work when opened straight from disk
    # (file:// blocks fetch), no web server needed
    (OUT.parent / "jobs.js").write_text(
        "window.JOBS_DATA = " + json.dumps(payload) + ";", encoding="utf-8")
    (OUT.parent / "desc.js").write_text(
        "window.DESC_DATA = " + json.dumps(descs) + ";", encoding="utf-8")
    flagged = sum(1 for j in unique if j.get("citizen_req"))
    print("Wrote %d unique jobs -> %s (%d source errors)" % (len(unique), OUT, len(errors)))
    print("Descriptions: %d | citizen/GC-flagged: %d" % (len(descs), flagged))


if __name__ == "__main__":
    main()
