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


def fetch_smartrecruiters(org, company):
    """SmartRecruiters public postings API. NOTE: returns HTTP 200 with an
    empty list for unknown slugs — emptiness is not an error signal."""
    out, offset = [], 0
    while offset < 500:
        r = requests.get("https://api.smartrecruiters.com/v1/companies/%s/postings" % company,
                         params={"limit": 100, "offset": offset}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json().get("content", [])
        if not items:
            break
        for j in items:
            title = j.get("name", "")
            if not wanted(title):
                continue
            loc = j.get("location") or {}
            city = loc.get("city") or ""
            country = (loc.get("country") or "").upper()
            url = "https://jobs.smartrecruiters.com/%s/%s" % (company, j.get("id"))
            posted = (j.get("releasedDate") or "")[:10] or None
            out.append(job(org, title, ("%s, %s" % (city, country)).strip(", "),
                           url, posted, "smartrecruiters"))
        offset += 100
    return out


# ---------------- abroad (Europe, relocation-feasible countries) ----------------
# Only countries where a non-EU hire is applicant-driven and realistic in 2026:
# Finland (specialist permit), Netherlands (HSM), Germany (Blue Card),
# Ireland (Critical Skills), Sweden. Estonia/Denmark excluded (quota / salary bar).
ABROAD_COUNTRIES = {
    "Finland": ("finland", "helsinki", "espoo", "tampere", "oulu", ", fi"),
    "Netherlands": ("netherlands", "amsterdam", "eindhoven", "rotterdam", "utrecht", ", nl"),
    "Germany": ("germany", "berlin", "munich", "münchen", "hamburg", ", de"),
    "Ireland": ("ireland", "dublin", ", ie"),
    "Sweden": ("sweden", "stockholm", "gothenburg", ", se"),
}
# stricter than wanted(): abroad list only carries data/analytics/AI roles
# that match the user's profile, not general software engineering
ABROAD_PROFILE_RE = re.compile(
    r"\b(data|analytics?|analyst|business intelligence|BI|insights?|"
    r"machine learning|ML|AI)\b", re.IGNORECASE)
RELO_RE = re.compile(r"relocat|visa (support|sponsorship|assistance)|work permit", re.I)


def abroad_country(location):
    low = (location or "").lower()
    for country, keys in ABROAD_COUNTRIES.items():
        if any(k in low for k in keys):
            return country
    return None


# ---- abroad academia: research roles needing a Master's (not PhD) ----
# doctoral researcher / PhD positions in FI/NL/SE/DE are salaried jobs with
# Master's entry; postdoc/professor/lecturer roles are excluded (need PhD)
ABROAD_ACADEMIC_RE = re.compile(
    r"\b(data|analytics?|analyst|machine learning|ML|AI|artificial intelligence|"
    r"statistic\w*|bioinformat\w*|computational|research software|data science|"
    r"deep learning|computer vision|NLP|research engineer)\b", re.IGNORECASE)
ACADEMIC_EXCLUDE_RE = re.compile(
    r"\b(postdoc\w*|post-doc\w*|professor|lecturer|group leader|"
    r"principal investigator|dean|docent)\b", re.IGNORECASE)


def _get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.text


def fetch_helsinki_uni(org):
    out, seen = [], set()
    for q in ("data", "analyst", "machine learning", "AI"):
        html = _get("https://jobs.helsinki.fi/search/", params={"q": q, "locale": "en_GB"})
        for m in re.finditer(
                r'<a[^>]*class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>\s*([^<]+)', html):
            path, title = m.group(1), m.group(2).strip()
            if path in seen:
                continue
            seen.add(path)
            out.append(job(org, title, "Helsinki, Finland",
                           "https://jobs.helsinki.fi" + path, None, "sf-html"))
    return out


def fetch_laura_rss(org, feed, location):
    xml = _get(feed)
    out = []
    for m in re.finditer(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>"
                         r".*?<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>.*?</item>",
                         xml, re.S):
        title = htmllib.unescape(m.group(1).strip())
        out.append(job(org, title, location, m.group(2).strip(), None, "laura-rss"))
    return out


def fetch_varbi(org, tenant, location):
    html = _get("https://%s.varbi.com/en/" % tenant)
    jobs_by_id = {}
    for m in re.finditer(r'href="(?:https?://[^"/]+)?(/en/what:job/jobID:(\d+)/[^"]*)"[^>]*>(.*?)</a>', html, re.S):
        text = WS_RE.sub(" ", TAG_RE.sub(" ", m.group(3))).strip()
        if not text:
            continue
        jobs_by_id.setdefault(m.group(2), {"path": m.group(1), "texts": []})["texts"].append(text)
    out = []
    for jid, info in jobs_by_id.items():
        texts = info["texts"]
        title = texts[0]
        deadline = next((t for t in texts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)), None)
        out.append(job(org, title, location,
                       "https://%s.varbi.com%s" % (tenant, info["path"]), None, "varbi"))
        if deadline:
            out[-1]["deadline"] = deadline
    return out


def fetch_academictransfer(org):
    out, seen = [], set()
    for q in ("data", "machine learning"):
        html = _get("https://www.academictransfer.com/en/jobs/", params={"q": q})
        for m in re.finditer(r'href="(/en/jobs/(\d+)/([^"/]+)/?)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{8,160})?', html):
            jid = m.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            title = (m.group(4) or "").strip()
            if not title or title.startswith("<"):
                title = m.group(3).replace("-", " ").strip().capitalize()
            out.append(job(org, title, "Netherlands (via AcademicTransfer)",
                           "https://www.academictransfer.com" + m.group(1), None, "at-html"))
    return out


def fetch_euraxess_r1(org, pages=10):
    """EURAXESS First Stage Researcher (R1) offers; country parsed per card.
    Rate-limited (429s) — pace requests and keep partial results."""
    import time
    out = []
    for p in range(pages):
        time.sleep(1.5)
        try:
            html = _get("https://euraxess.ec.europa.eu/jobs/search",
                        params={"f[0]": "job_research_profile:447", "page": p})
        except Exception:
            break  # rate limited — keep what we have
        blocks = html.split("<article")[1:]
        if not blocks:
            break
        for b in blocks:
            tm = re.search(r'href="(/jobs/\d+)"[^>]*>\s*([^<]+)', b)
            if not tm:
                continue
            country = next((c for c in ABROAD_COUNTRIES
                            if re.search(r"\b" + c + r"\b", b)), None)
            if not country:
                continue
            out.append(job(org, tm.group(2).strip(), country,
                           "https://euraxess.ec.europa.eu" + tm.group(1), None, "euraxess"))
    return out


def fetch_mpg_rss(org):
    xml = _get("https://www.mpg.de/feeds/jobs.rss")
    out = []
    for m in re.finditer(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>"
                         r".*?<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>.*?</item>", xml, re.S):
        out.append(job(org, htmllib.unescape(m.group(1).strip()), "Germany (Max Planck)",
                       m.group(2).strip(), None, "mpg-rss"))
    return out


def fetch_hida(org):
    html = _get("https://www.helmholtz-hida.de/en/jobs/job-offers/")
    out = []
    for m in re.finditer(r'<a[^>]*class="[^"]*list-item-job[^"]*"[^>]*href="([^"]+)"(.*?)</a>',
                         html, re.S):
        text = WS_RE.sub(" ", TAG_RE.sub(" ", m.group(2))).strip()
        if not text:
            continue
        url = m.group(1)
        if url.startswith("/"):
            url = "https://www.helmholtz-hida.de" + url
        out.append(job(org, text[:140], "Germany (Helmholtz)", url, None, "hida"))
    return out


# (org, fetcher, args, fixed country or None to detect from location)
ABROAD_ACADEMIC_SOURCES = [
    ("Aalto University", fetch_workday, ("aalto.wd3.myworkdayjobs.com", "aalto", "aalto"), "Finland"),
    ("University of Helsinki", fetch_helsinki_uni, (), "Finland"),
    ("Tampere University", fetch_laura_rss, ("https://tuni.rekrytointi.com/paikat/index.php?o=A_RSS&jgid=3", "Tampere, Finland"), "Finland"),
    ("University of Oulu", fetch_varbi, ("oulunyliopisto", "Oulu, Finland"), "Finland"),
    ("KTH Royal Institute of Technology", fetch_varbi, ("kth", "Stockholm, Sweden"), "Sweden"),
    ("Karolinska Institutet", fetch_varbi, ("ki", "Stockholm, Sweden"), "Sweden"),
    ("Uppsala University", fetch_varbi, ("uu", "Uppsala, Sweden"), "Sweden"),
    ("Stockholm University", fetch_varbi, ("su", "Stockholm, Sweden"), "Sweden"),
    ("Lund University", fetch_varbi, ("lu", "Lund, Sweden"), "Sweden"),
    ("AcademicTransfer (NL universities)", fetch_academictransfer, (), "Netherlands"),
    ("EURAXESS (EU research portal)", fetch_euraxess_r1, (), None),
    ("Max Planck Society", fetch_mpg_rss, (), "Germany"),
    ("Helmholtz HIDA", fetch_hida, (), "Germany"),
]


ABROAD_SOURCES = [
    ("Wolt", fetch_greenhouse, ("wolt",)),
    ("Supercell", fetch_ashby, ("supercell",)),
    ("Smartly.io", fetch_greenhouse, ("smartlyio",)),
    ("Oura", fetch_greenhouse, ("oura",)),
    ("Adyen", fetch_greenhouse, ("adyen",)),
    ("Mollie", fetch_ashby, ("mollie",)),
    ("GetYourGuide", fetch_greenhouse, ("getyourguide",)),
    ("N26", fetch_greenhouse, ("n26",)),
    ("HelloFresh", fetch_greenhouse, ("hellofresh",)),
    ("Celonis", fetch_greenhouse, ("celonis",)),
    ("Intercom", fetch_greenhouse, ("intercom",)),
    ("Stripe", fetch_greenhouse, ("stripe",)),
    ("Spotify", fetch_lever, ("spotify",)),
    ("Delivery Hero", fetch_smartrecruiters, ("DeliveryHero",)),
    ("Workhuman", fetch_workday, ("workhuman.wd1.myworkdayjobs.com", "workhuman", "WorkhumanCareers")),
]


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

    for org, fn, args in ABROAD_SOURCES:
        try:
            found = fn(org, *args)
            kept = []
            for j in found:
                country = abroad_country(j.get("location"))
                if not country or not ABROAD_PROFILE_RE.search(j["title"]):
                    continue
                j["abroad"] = country
                j["sector"] = "Tech / Software"
                if RELO_RE.search(j.get("_desc") or ""):
                    j["relo"] = True
                kept.append(j)
            print("  %-34s %3d roles (abroad)" % (org, len(kept)))
            all_jobs.extend(kept)
        except Exception as e:  # noqa: BLE001
            print("  %-34s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})

    for org, fn, args, fixed_country in ABROAD_ACADEMIC_SOURCES:
        try:
            found = fn(org, *args)
            kept = []
            for j in found:
                title = j["title"]
                if ACADEMIC_EXCLUDE_RE.search(title) or not ABROAD_ACADEMIC_RE.search(title):
                    continue
                country = fixed_country or abroad_country(j.get("location"))
                if not country:
                    continue
                j["abroad"] = country
                j["sector"] = "Academia & Research"
                kept.append(j)
            print("  %-34s %3d roles (abroad academia)" % (org, len(kept)))
            all_jobs.extend(kept)
        except Exception as e:  # noqa: BLE001
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
