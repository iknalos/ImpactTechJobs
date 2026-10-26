"""ImpactTech Jobs scraper.

Pulls current job postings from the ATS APIs of elder-support / aging-services
organizations, keeps only data / IT / AI / software roles, merges in manually
curated postings (manual_jobs.json), and writes docs/jobs.json for the
dashboard (docs/index.html).

Run: python scraper.py
"""

import html as htmllib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "jobs.json"
DESC_OUT = ROOT / "docs" / "desc.json"
MANUAL = ROOT / "manual_jobs.json"
ACWIA = ROOT / "data" / "acwia_employers.json"  # built by tools/build_acwia.py
CAP_ORGS = ROOT / "cap_exempt_orgs.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (ImpactTechJobs job dashboard; personal use)"}
TIMEOUT = 30

# ---------------------------------------------------------------- filtering

INCLUDE = re.compile(
    r"\b(data|analyst|analytics|engineer(ing)?|software|developer|dev\s?ops|"
    r"database|informatics?|informaticist|scientist|business intelligence|"
    r"machine learning|artificial intelligence|cloud|platform|digital|"
    r"technology|technical|systems?|prompt|salesforce|tableau|power\s?bi|"
    # small agencies' data/IT roles rarely say "data": QI, reporting, help desk
    r"quality improvement|reporting|evaluation|evaluator|programmer|HRIS|cyber\w*|"
    r"help\s?desk|service\s?desk|network (?:administrator|engineer|specialist|technician|analyst)|"
    r"AI|ML|BI|IT|IAM|SRE|CRM|EHR|EMR|GIS)\b",
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


DEAD_TEXT_RE = re.compile(
    r"no longer (?:available|accepting|active|open|posted)|"
    r"(?:position|job|posting|requisition) (?:has been|is|was) (?:filled|closed|removed)|"
    r"job (?:is )?not found|(?:job|posting|position|requisition) (?:has |is )?(?:expired|closed)|"
    r"position (?:is )?(?:no longer|not) available",
    re.I)
DEAD_URL_RE = re.compile(r"not-?found|not-?avail|no-?longer|expired", re.I)
SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
TITLE_STOP = {"and", "the", "for", "with", "remote", "senior", "sr", "lead", "job", "id"}


def _norm(s):
    return WS_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def manual_alive(m):
    """Hand-entered postings never refresh themselves, so check each one is
    still up. Dead = 404/410, redirected to a not-found page, the page says
    the job closed, or a real (non-JS-shell) page no longer contains the
    title. A network error keeps the entry: one flaky site shouldn't wipe it."""
    try:
        r = requests.get(m["url"], headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return True
    if r.status_code in (404, 410):
        return False
    if r.url != m["url"] and DEAD_URL_RE.search(r.url):
        return False
    if DEAD_TEXT_RE.search(r.text):
        return False
    # word-level, not exact phrase: hand-typed titles abbreviate and use "&"
    # where the page says "and"
    text = " %s " % _norm(TAG_RE.sub(" ", SCRIPT_RE.sub(" ", r.text)))
    words = [w for w in _norm(re.sub(r"\(.*?\)", " ", m["title"])).split()
             if len(w) >= 2 and w not in TITLE_STOP]
    return len(text) < 1500 or all(" %s " % w in text for w in words)


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

# postings each board returned before the title filter, for source health:
# a board that suddenly returns nothing at all has moved, while a small
# agency with no tech openings this month is fine
BOARD_TOTALS = {}


def _seen(org, n):
    BOARD_TOTALS[org] = BOARD_TOTALS.get(org, 0) + n


def _retry_get(url, tries=3, **kw):
    """GET with backoff, for ATS hosts that time out or 404 at random (ADP)."""
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", TIMEOUT)
    for k in range(tries):
        try:
            r = requests.get(url, **kw)
            if r.ok or k == tries - 1:
                r.raise_for_status()
                return r
        except requests.RequestException:
            if k == tries - 1:
                raise
        time.sleep(2 * (k + 1))

def fetch_greenhouse(org, board, *more_boards):
    """Greenhouse job board API; some employers run one board per site."""
    jobs = []
    for b in (board,) + more_boards:
        r = requests.get(
            "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % b,
            headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        jobs += r.json().get("jobs", [])
    out = []
    _seen(org, len(jobs))
    for j in jobs:
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
    jobs = r.json()
    _seen(org, len(jobs))
    for j in jobs:
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
    jobs = r.json().get("jobs", [])
    _seen(org, len(jobs))
    for j in jobs:
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
        _seen(org, len(jobs))
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


# Workday tenants share server pools (*.wd1, *.wd5 ...) that answer 429 when
# several parallel sources hit them at once: cap concurrent calls and back off
WORKDAY_LANE = threading.Semaphore(2)


def _workday_call(method, url, **kw):
    for k in range(5):
        with WORKDAY_LANE:
            r = requests.request(method, url, timeout=TIMEOUT,
                                 headers=dict(HEADERS, Accept="application/json"), **kw)
        if r.status_code not in (429, 500, 502, 503, 504) or k == 4:
            r.raise_for_status()
            return r
        wait = r.headers.get("Retry-After", "")
        time.sleep(min(60, int(wait)) if wait.isdigit() else 5 * (k + 1))


def fetch_workday(org, host, tenant, site, terms=None, facets=None):
    """Workday CXS JSON API.

    Searches per keyword instead of dumping the whole board: large tenants
    have thousands of postings, and some ignore the offset parameter and
    return page 1 forever — the seen-path check below breaks that loop.
    terms/facets scope a brand inside a parent's tenant (Oak Street Health
    inside CVS: searchText "Oak Street Health" + the data/tech job families).
    """
    url = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
    seen, out = set(), []
    for term in terms or WD_TERMS:
        offset = 0
        while offset < 200:
            body = {"appliedFacets": facets or {}, "limit": 20, "offset": offset, "searchText": term}
            postings = _workday_call("POST", url, json=body).json().get("jobPostings", [])
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
    _seen(org, len(seen))
    # second pass: fetch full descriptions only for the jobs that matched
    for j in out:
        path, j["_desc"] = j["_desc"], ""
        try:
            r = _workday_call("GET", "https://%s/wday/cxs/%s/%s%s" % (host, tenant, site, path))
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
    _seen(org, len(items or []))
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


ADP_API = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
           "staffing/v1/job-requisitions")


def fetch_adp(org, cid):
    """ADP Workforce Now career center: the JSON behind
    recruitment.html?cid=<cid>. Small agencies and PACE programs live here.
    Pages of 20 via $skip; descriptions take one GET per matched job."""
    params = {"cid": cid, "ccId": "19000101_000001", "lang": "en_US", "locale": "en_US"}
    hdrs = dict(HEADERS, Accept="application/json")
    out, skip, total = [], 0, 1
    while skip < total and skip < 1000:
        d = _retry_get(ADP_API, params=dict(params, **{"$top": 20, "$skip": skip}),
                       headers=hdrs).json()
        total = (d.get("meta") or {}).get("totalNumber") or 0
        reqs = d.get("jobRequisitions") or []
        if not reqs:
            break
        _seen(org, len(reqs))
        for q in reqs:
            title = q.get("requisitionTitle") or ""
            if not wanted(title):
                continue
            ext = next((f.get("stringValue") for f in
                        (q.get("customFieldGroup") or {}).get("stringFields", [])
                        if (f.get("nameCode") or {}).get("codeValue") == "ExternalJobID"), None)
            locs = q.get("requisitionLocations") or [{}]
            loc = ((locs[0].get("nameCode") or {}).get("shortName") or "").strip()
            loc = re.sub(r",\s*US$", "", loc)
            url = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                   "?cid=%s&ccId=19000101_000001&jobId=%s&lang=en_US" % (cid, ext or q["itemID"]))
            desc = ""
            try:
                dr = requests.get("%s/%s" % (ADP_API, q["itemID"]), params=params,
                                  headers=hdrs, timeout=TIMEOUT)
                dr.raise_for_status()
                desc = strip_html(dr.json().get("requisitionDescription", ""))
            except Exception:
                pass
            out.append(job(org, title, loc, url, (q.get("postDate") or "")[:10] or None,
                           "adp", desc))
        skip += 20
    return out


def _us_date(s, fmt):
    try:
        return datetime.strptime((s or "").strip(), fmt).date().isoformat()
    except ValueError:
        return None


def fetch_applicantpro(org, base):
    """ApplicantPro, also white-labelled as isolvedhire.com / applicantpool.com.
    The board's domain_id is in the /jobs/ page; the JSON endpoint returns a
    PHP error page unless getParams is passed."""
    did = re.search(r"var domain_id = '(\d+)'", _get(base + "/jobs/")).group(1)
    r = _retry_get("%s/core/jobs/%s" % (base, did), params={"getParams": "{}"})
    jobs = r.json()["data"]["jobs"]
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = htmllib.unescape(j.get("title") or "")
        if not wanted(title):
            continue
        out.append(job(org, title, j.get("jobLocation") or j.get("city"),
                       j.get("jobUrl") or "%s/jobs/%s" % (base, j["id"]),
                       _us_date(j.get("startDateRef"), "%b %d, %Y"), "applicantpro"))
    return out


PAYLOCITY_DATA_RE = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", re.S)


def fetch_paylocity(org, guid):
    """Paylocity Recruiting: the board's JSON is inlined as window.pageData.
    (The v2 feed API answers 200 with an empty list for these boards.)"""
    page = _get("https://recruiting.paylocity.com/recruiting/jobs/All/%s" % guid)
    m = PAYLOCITY_DATA_RE.search(page)
    if not m:  # some tenants (Benjamin Rose) only expose the v2 feed
        return _paylocity_feed(org, guid)
    jobs = [j for j in json.loads(m.group(1)).get("Jobs", []) if not j.get("IsInternal")]
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = j.get("JobTitle") or ""
        if not wanted(title):
            continue
        loc = j.get("JobLocation") or {}
        where = "Remote" if j.get("IsRemote") else ", ".join(
            x for x in (loc.get("City"), loc.get("State")) if x)
        out.append(job(org, title, where,
                       "https://recruiting.paylocity.com/Recruiting/Jobs/Details/%s" % j["JobId"],
                       (j.get("PublishedDate") or "")[:10] or None, "paylocity",
                       strip_html(j.get("Description", ""))))
    return out


def _paylocity_feed(org, guid):
    r = _retry_get("https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/%s" % guid,
                   headers=dict(HEADERS, Accept="application/json"))
    jobs = r.json().get("jobs") or []
    _seen(org, len(jobs))
    return [job(org, j["title"].strip(), (j.get("jobLocation") or {}).get("locationDisplayName"),
                j.get("displayUrl"), (j.get("publishedDate") or "")[:10] or None, "paylocity",
                strip_html(j.get("description", "")))
            for j in jobs if wanted(j.get("title") or "")]


def fetch_jazzhr(org, subdomain):
    """JazzHR public XML export of open jobs (no posting dates in it)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_retry_get("https://app.jazz.co/feeds/export/jobs/%s" % subdomain).content)
    jobs = [j for j in root.findall("job") if (j.findtext("status") or "Open") == "Open"]
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = (j.findtext("title") or "").strip()
        if not wanted(title):
            continue
        where = ", ".join(x for x in (j.findtext("city"), j.findtext("state")) if x)
        out.append(job(org, title, where, j.findtext("url"), None, "jazzhr",
                       strip_html(j.findtext("description") or "")))
    return out


PAYCOM_SEARCH = ("https://portal-applicant-tracking.us-cent.paycomonline.net"
                 "/api/ats/job-posting-previews/search")


def fetch_paycom(org, clientkey):
    """Paycom ATS: the career page hands out a short-lived session JWT, which
    the search API takes as a raw Authorization header (no "Bearer")."""
    portal = "https://www.paycomonline.net/v4/ats/web.php/portal/%s" % clientkey
    jwt = re.search(r'"sessionJWT":"([^"]+)"', _get(portal + "/career-page")).group(1)
    body = {"skip": 0, "take": 100, "filtersForQuery": {
        "distanceFrom": 0, "workEnvironments": [], "positionTypes": [], "educationLevels": [],
        "categories": [], "travelTypes": [], "shiftTypes": [], "otherFilters": [],
        "keywordSearchText": "", "location": "", "sortOption": "N"}}
    r = requests.post(PAYCOM_SEARCH, json=body, timeout=TIMEOUT,
                      headers=dict(HEADERS, Authorization=jwt, Locale="en-US"))
    r.raise_for_status()
    jobs = r.json().get("jobPostingPreviews") or []
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = (j.get("jobTitle") or "").strip()
        if not wanted(title):
            continue
        out.append(job(org, title, j.get("locations"), "%s/jobs/%s" % (portal, j["jobId"]),
                       (j.get("postedOn") or "")[:10] or None, "paycom",
                       strip_html(j.get("description", ""))))
    return out


def fetch_rss(org, feed, location=""):
    """Generic RSS job feed (HiringThing / Checkwriters Recruit and similar)."""
    import xml.etree.ElementTree as ET
    items = [i for i in ET.fromstring(_retry_get(feed).content).iter("item")
             if not re.search(r"\binternal (posting|only)\b",
                              (i.findtext("title") or "") + " " + (i.findtext("description") or ""), re.I)]
    _seen(org, len(items))
    out = []
    for i in items:
        title = (i.findtext("title") or "").strip()
        if not wanted(title):
            continue
        out.append(job(org, title, i.findtext("location") or location, i.findtext("link"),
                       None, "rss", strip_html(i.findtext("description") or "")))
    return out


def fetch_wp_category(org, site, slug, location):
    """Agencies that post each opening as a WordPress post in one category."""
    cat = _retry_get("%s/wp-json/wp/v2/categories" % site, params={"slug": slug}).json()[0]["id"]
    posts = _retry_get("%s/wp-json/wp/v2/posts" % site, params={
        "categories": cat, "per_page": 100, "_fields": "date,link,title,content"}).json()
    _seen(org, len(posts))
    out = []
    for p in posts:
        title = strip_html(p["title"]["rendered"])
        if not wanted(title):
            continue
        # Divi page-builder shortcodes ([et_pb_...]) wrap the real text
        desc = re.sub(r"\[/?et_pb[^\]]*\]", " ", strip_html(p.get("content", {}).get("rendered", "")))
        out.append(job(org, title, location, p["link"], p["date"][:10], "wordpress", desc))
    return out


def fetch_dejobs(org, company):
    """Indeed-only employers (Indeed blocks scripts): DirectEmployers / NLx
    re-posts from the state job bank, via dejobs.org's JSON feed. A floor,
    not the full board, and dated when the state bank picked them up."""
    r = _retry_get("https://dejobs.org/jobs/feed/json",
                   params={"q": '"%s"' % company, "num_items": 100})
    jobs = [j for j in r.json() if (j.get("company") or "").upper().startswith(company.upper())]
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = (j.get("title") or "").strip()
        if not wanted(title):
            continue
        out.append(job(org, title, j.get("location"), j.get("url"),
                       (j.get("date_new") or "")[:10] or None, "dejobs",
                       strip_html(j.get("description", ""))))
    return out


def fetch_ultipro(org, tenant, board, host="recruiting.ultipro.com"):
    """UKG Pro (UltiPro) recruiting board: the JSON search behind the page.
    Tenants live on recruiting.ultipro.com or recruiting2.ultipro.com."""
    url = "https://%s/%s/JobBoard/%s/JobBoardView/LoadSearchResults" % (host, tenant, board)
    out, skip, total = [], 0, 1
    while skip < total and skip < 2000:
        body = {"opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": "", "Filters": [],
                                      "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                                                   "Ascending": False}]},
                "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                                  "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}
        r = requests.post(url, json=body, timeout=TIMEOUT, headers=dict(HEADERS, Accept="application/json"))
        r.raise_for_status()
        d = r.json()
        total = d.get("totalCount") or 0
        ops = d.get("opportunities") or []
        if not ops:
            break
        _seen(org, len(ops))
        for o in ops:
            title = (o.get("Title") or "").strip()
            if not wanted(title):
                continue
            addr = ((o.get("Locations") or [{}])[0].get("Address") or {})
            where = ", ".join(x for x in (addr.get("City"), (addr.get("State") or {}).get("Code")) if x)
            out.append(job(org, title, where,
                           "https://%s/%s/JobBoard/%s/OpportunityDetail?opportunityId=%s"
                           % (host, tenant, board, o["Id"]),
                           (o.get("PostedDate") or "")[:10] or None, "ultipro",
                           o.get("BriefDescription") or ""))
        skip += 50
    return out


ICIMS_ROW_RE = re.compile(r'<a[^>]*href="([^"]*/jobs/(\d+)/[^"]*)"[^>]*class="iCIMS_Anchor"[^>]*title="\d+\s*-\s*([^"]*)"'
                          r'|<a[^>]*class="iCIMS_Anchor"[^>]*href="([^"]*/jobs/(\d+)/[^"]*)"[^>]*title="\d+\s*-\s*([^"]*)"')


def fetch_icims(org, host):
    """Classic iCIMS portal, server-rendered list (in_iframe=1), paged by pr."""
    out, seen, pr, pages = [], set(), 0, 1
    while pr < pages and pr < 80:
        page = _get("https://%s/jobs/search" % host, params={"ss": 1, "in_iframe": 1, "pr": pr})
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", page)
        pages = int(m.group(1)) if m else 1
        new = 0
        for g in ICIMS_ROW_RE.findall(page):
            href, jid, title = (g[0], g[1], g[2]) if g[0] else (g[3], g[4], g[5])
            if jid in seen:
                continue
            seen.add(jid)
            new += 1
            title = htmllib.unescape(title).strip()
            if wanted(title):
                out.append(job(org, title, "", href.split("?")[0], None, "icims"))
        if not new:
            break
        pr += 1
    _seen(org, len(seen))
    return out


TALEO_FILTERS = ["POSTING_DATE", "LOCATION", "JOB_FIELD", "ORGANIZATION"]
TALEO_ADV = ["ORGANIZATION", "LOCATION", "JOB_FIELD", "URGENT_JOB", "EMPLOYEE_STATUS", "JOB_SCHEDULE", "JOB_TYPE"]
AGING_ORG_RE = re.compile(r"\b(aging|elder)", re.I)


def fetch_taleo(org, host, portal, section, prefix):
    """Oracle Taleo Enterprise careersection REST (MassCareers = every MA
    executive-branch agency). Rows are columns: title, schedule, job field,
    agency, locations (JSON), posted. One org per hiring agency."""
    url = "https://%s/careersection/rest/jobboard/searchjobs" % host
    hdrs = dict(HEADERS, Accept="application/json", tz="GMT-05:00", tzname="America/New_York")
    out, page, got, total = [], 1, 0, 1
    while got < total and page <= 80:
        body = {"multilineEnabled": True, "pageNo": page,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": "", "ORGANIZATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": [
                    {"id": f, "selectedValues": []} for f in TALEO_FILTERS]},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
                    {"id": f, "selectedValues": []} for f in TALEO_ADV]}}
        r = requests.post(url, params={"lang": "en", "portal": portal}, json=body,
                          headers=hdrs, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        total = (d.get("pagingData") or {}).get("totalCount") or 0
        rows = d.get("requisitionList") or []
        if not rows:
            break
        got += len(rows)
        _seen(org, len(rows))
        for row in rows:
            col = row.get("column") or []
            title = (col[0] if col else "").strip()
            if not wanted(title):
                continue
            agency = col[3].strip() if len(col) > 3 else ""
            try:
                where = "; ".join(json.loads(col[4])).replace("Massachusetts-", "") + ", MA"
            except (IndexError, ValueError, TypeError):
                where = "Massachusetts"
            j = job("%s%s" % (prefix, agency or org), title, where,
                    "https://%s/careersection/%s/jobdetail.ftl?job=%s" % (host, section, row.get("contestNo")),
                    _us_date(col[5] if len(col) > 5 else "", "%b %d, %Y"), "taleo")
            j["sector"] = "Aging Services" if AGING_ORG_RE.search(agency) else "Government & Public Sector"
            out.append(j)
        page += 1
    return out


def fetch_neogov(org, agency, dept_re=None):
    """NEOGOV / GovernmentJobs.com agency RSS (complete, unlike the paged
    HTML list); each item names its department. dept_re keeps one
    department of a big county board."""
    xml = _retry_get("https://www.governmentjobs.com/SearchEngine/JobsFeed",
                     params={"agency": agency}).content.decode("utf-8-sig", "replace")
    items = re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.S)
    if not items:  # some agencies (Atlanta Regional) publish no RSS
        return _neogov_html(org, agency, dept_re)
    _seen(org, len(items))
    out = []
    for it in items:
        tag = lambda t: htmllib.unescape((re.search(r"<%s\b[^>]*>(.*?)</%s>" % (t, t), it, re.S)  # noqa: E731
                                         or re.match("()", "")).group(1)).strip()
        title = tag("title")
        dept = tag("joblisting:department")
        if not wanted(title) or (dept_re and not re.search(dept_re, dept, re.I)):
            continue
        posted = _us_date(tag("pubDate")[:16], "%a, %d %b %Y")
        j = job(org + (" - " + dept if dept else ""), title, tag("joblisting:location") or "Boston, MA",
                tag("link"), posted, "neogov", strip_html(tag("description")))
        j["sector"] = "Aging Services" if AGING_ORG_RE.search(dept) or "age strong" in dept.lower() \
            else "Government & Public Sector"
        out.append(j)
    return out


def fetch_phenom(org, base, refnum, page_id, selected=None):
    """Phenom People career sites: the site's own refineSearch widget call.
    selected narrows server-side, e.g. {"category": ["Technology ..."]}."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(base + "/us/en/search-results", timeout=TIMEOUT)  # session cookie
    out, frm, total = [], 0, 1
    while frm < total and frm < 1000:
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": frm, "jobs": True,
                "counts": True, "all_fields": ["category", "country", "state", "city", "type"],
                "size": 100, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": page_id, "siteType": "external", "keywords": "", "global": True,
                "selected_fields": selected or {}, "locationData": {}, "refNum": refnum}
        r = s.post(base + "/widgets", json=body, timeout=TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        rs = r.json().get("refineSearch") or {}
        total = rs.get("totalHits") or 0
        jobs = (rs.get("data") or {}).get("jobs") or []
        if not jobs:
            break
        _seen(org, len(jobs))
        for j in jobs:
            title = (j.get("title") or "").strip()
            if not wanted(title):
                continue
            out.append(job(org, title, j.get("cityStateCountry") or j.get("location"),
                           "%s/us/en/job/%s" % (base, j.get("jobId")),
                           (j.get("postedDate") or "")[:10] or None, "phenom",
                           j.get("descriptionTeaser") or ""))
        frm += len(jobs)
    return out


def fetch_ukg_ready(org, host, company):
    """UKG Ready (Kronos Workforce Ready / saashr): REST list behind
    <host>/ta/<company>.careers. The company id needs a literal %7C (|)."""
    url = "https://%s/ta/rest/ui/recruitment/companies/%%7C%s/job-requisitions" % (host, company)
    r = _retry_get(url, params={"offset": 0, "size": 100, "sort": "desc", "ein_id": "", "lang": "en-US"},
                   headers=dict(HEADERS, Accept="application/json"))
    jobs = r.json().get("job_requisitions") or []
    _seen(org, len(jobs))
    out = []
    for j in jobs:
        title = (j.get("job_title") or "").strip()
        if not wanted(title):
            continue
        loc = j.get("location") or {}
        out.append(job(org, title, ", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
                       "https://%s/ta/%s.careers?ShowJob=%s" % (host, company, j["id"]), None,
                       "ukg-ready", j.get("job_description") or ""))
    return out


def fetch_nyc_jobs(org, agency):
    """NYC Open Data 'Jobs NYC Postings' (Socrata kpav-sd4t); refreshed about
    weekly. Every job is listed twice (Internal + External): keep External."""
    r = _retry_get("https://data.cityofnewyork.us/resource/kpav-sd4t.json",
                   params={"$where": "agency='%s'" % agency, "$limit": 1000, "$order": "posting_date DESC"})
    rows, seen = [], set()
    for x in r.json():
        if x.get("job_id") in seen or x.get("posting_type", "External") != "External":
            continue
        seen.add(x.get("job_id"))
        rows.append(x)
    _seen(org, len(rows))
    out = []
    for x in rows:
        title = (x.get("business_title") or x.get("civil_service_title") or "").strip()
        if not wanted(title):
            continue
        url = ("https://a127-jobs.nyc.gov/psc/nycjobs/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
               "?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=1&JobOpeningId=%s&PostingSeq=1"
               % x["job_id"])
        desc = " ".join(x.get(k, "") for k in ("job_description", "minimum_qual_requirements",
                                                "preferred_skills", "additional_information"))
        out.append(job(org, title, (x.get("work_location") or "New York, NY") + ", NY", url,
                       (x.get("posting_date") or "")[:10] or None, "nyc-opendata", desc))
    return out


def fetch_oracle_hcm(org, host, site="CX_1"):
    """Oracle Recruiting Cloud candidate experience REST (Taleo's successor)."""
    out, off, total = [], 0, 1
    while off < total and off < 3000:
        r = _retry_get("https://%s/hcmRestApi/resources/latest/recruitingCEJobRequisitions" % host, params={
            "onlyData": "true", "expand": "requisitionList.secondaryLocations",
            "finder": "findReqs;siteNumber=%s,limit=200,offset=%d,sortBy=POSTING_DATES_DESC" % (site, off)})
        it = (r.json().get("items") or [{}])[0]
        total = it.get("TotalJobsCount") or 0
        reqs = it.get("requisitionList") or []
        if not reqs:
            break
        _seen(org, len(reqs))
        for q in reqs:
            title = (q.get("Title") or "").strip()
            if not wanted(title):
                continue
            out.append(job(org, title, q.get("PrimaryLocation"),
                           "https://%s/hcmUI/CandidateExperience/en/sites/%s/job/%s" % (host, site, q["Id"]),
                           q.get("PostedDate"), "oracle-hcm", q.get("ShortDescriptionStr") or ""))
        off += 200
    return out


def fetch_rippling(org, slug):
    r = _retry_get("https://api.rippling.com/platform/api/ats/v1/board/%s/jobs" % slug)
    jobs = r.json()
    _seen(org, len(jobs))
    return [job(org, j["name"].strip(), (j.get("workLocation") or {}).get("label"), j.get("url"),
                None, "rippling")
            for j in jobs if wanted(j.get("name") or "")]


def fetch_link_list(org, url, marker, link_re, location=""):
    """Static job lists where each opening is a link: link_re captures
    (href, title). Used for association job boards (USAging, ...). The
    marker is text the page always has, so an empty list is told apart
    from a redesign (which raises)."""
    from urllib.parse import urljoin
    page = _get(url)
    if marker not in page:
        raise RuntimeError("marker %r not found: page redesigned?" % marker)
    pairs = {}
    for href, title in re.findall(link_re, page, re.S | re.I):
        title = strip_html(title)
        if title:
            pairs.setdefault(urljoin(url, htmllib.unescape(href)), title)
    _seen(org, len(pairs))
    return [job(org, t, location, u, None, "html") for u, t in pairs.items() if wanted(t)]


def fetch_peopleclick(org, client):
    """PeopleFluent / PeopleClick RWD (MIT). getJobs answers "Session
    Timeout" unless the search form was posted first in the same session."""
    s = requests.Session()
    s.headers.update(HEADERS)
    base = "https://careers.peopleclick.com/careerscp/%s/external/" % client
    s.get(base + "search/search.html", timeout=TIMEOUT).raise_for_status()
    pre = "com.peopleclick.cp.formdata."
    form = {"foo": "", "input": "Search", pre + "hitsPerPage": "50"}
    form.update({pre + f: "" for f in ("FLD_JP_POSITION_CATEGORY", "FLD_JP_DEPARTMENT",
                                       "FLD_JPM_DIVISION", "JPM_DURATION", "SEARCHCRITERIA_JOBPOSTAGE")})
    s.post(base + "results/searchResult.html", data=form, timeout=TIMEOUT).raise_for_status()
    api = "https://careers.peopleclick.com/careerscp/api/%s/external/en_US/getJobs" % client
    out, seen, page, total = [], 0, 1, 1
    while seen < total and page < 60:
        d = s.get(api, params={"sortBy": "", "isAscending": "false", "page": page}, timeout=TIMEOUT,
                  headers={"Accept": "application/json"}).json()
        if d.get("status") == "fail":
            raise RuntimeError("PeopleClick: %s" % d.get("errorMsg"))
        total = int(d.get("totalHits") or 0)
        jobs = d.get("jobList") or []
        if not jobs:
            break
        seen += len(jobs)
        _seen(org, len(jobs))
        for jb in jobs:
            a = jb.get("attributes") or {}
            title = (a.get("JPM_TITLE") or a.get("FLD_JP_POSTING_TITLE") or "").strip()
            if not wanted(title):
                continue
            out.append(job(org, title, "Cambridge, MA",
                           base + "jobDetails/jobDetail.html?jobPostId=%s&localeCode=en-us" % jb.get("jobPostId"),
                           (str(a.get("JPM_CREATEDON") or ""))[:10] or None, "peopleclick"))
        page += 1
    return out


def fetch_brassring(org, partner, site, host="sjobs.brassring.com"):
    """IBM Kenexa BrassRing TGnewUI: two JSON POSTs that need the page's
    anti-forgery token (RFT header) and its encrypted session value."""
    s = requests.Session()
    s.headers.update(HEADERS)
    home = "https://%s/TGnewUI/Search/Home/Home?partnerid=%s&siteid=%s" % (host, partner, site)
    page = s.get(home, timeout=TIMEOUT).text
    cookie = re.search(r'id="CookieValue" type="hidden" value="([^"]*)"', page).group(1)
    rvt = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]*)"', page).group(1)
    kf = (re.search(r"KeywordCustomSolrFields&quot;:&quot;([^&]*)&quot;", page) or re.match("()", "")).group(1)
    lf = (re.search(r"LocationCustomSolrFields&quot;:&quot;([^&]*)&quot;", page) or re.match("()", "")).group(1)
    hdr = {"RFT": rvt, "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
           "X-Requested-With": "XMLHttpRequest", "Origin": "https://" + host, "Referer": home}
    body = {"PartnerId": partner, "SiteId": site, "Keyword": "", "Location": "",
            "KeywordCustomSolrFields": kf, "LocationCustomSolrFields": lf, "FacetFilterFields": None,
            "TurnOffHttps": False, "Latitude": 0, "Longitude": 0,
            "PowerSearchOptions": {"PowerSearchOption": []}, "encryptedsessionvalue": cookie}
    r = s.post("https://%s/TgNewUI/Search/Ajax/MatchedJobs" % host, data=json.dumps(body),
               headers=hdr, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    count = d.get("JobsCount") or 0
    jobs = list((d.get("Jobs") or {}).get("Job") or [])
    pg = 1
    while len(jobs) < count and pg < 100:
        pg += 1
        more = {"partnerId": partner, "siteId": site, "keyword": "", "location": "",
                "keywordCustomSolrFields": kf, "locationCustomSolrFields": lf, "linkId": "",
                "Latitude": 0, "Longitude": 0, "facetfilterfields": {"Facet": []},
                "powersearchoptions": {"PowerSearchOption": []}, "SortType": "LastUpdated",
                "pageNumber": pg, "encryptedSessionValue": cookie}
        r = s.post("https://%s/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs" % host,
                   data=json.dumps(more), headers=hdr, timeout=TIMEOUT)
        r.raise_for_status()
        batch = (r.json().get("Jobs") or {}).get("Job") or []
        if not batch:
            break
        jobs += batch
        time.sleep(0.3)
    _seen(org, len(jobs))
    out = []
    for jb in jobs:
        q = {x.get("QuestionName", "").lower(): x.get("Value", "") for x in jb.get("Questions", [])}
        title = htmllib.unescape(q.get("jobtitle", "")).strip()
        if not wanted(title):
            continue
        out.append(job(org, title, q.get("formtext13") or "Boston, MA",
                       "https://%s/TGnewUI/Search/home/HomeWithPreLoad?PageType=JobDetails"
                       "&partnerid=%s&siteid=%s&jobid=%s" % (host, partner, site, q.get("reqid")),
                       _us_date(q.get("lastupdated", "")[:10], "%m/%d/%Y"), "brassring"))
    return out


SILKROAD_RE = re.compile(r'id="Jobs_PagedJobList_JobTitle-(\d+)"[^>]*>(.*?)</div>.*?'
                         r'sr-panel__location[^>]*>(.*?)</div>', re.S)


def fetch_silkroad(org, tenant, portal):
    """SilkRoad OpenHire: server-rendered, 10 per page, 'Page 1 of N'."""
    base = "https://jobs.silkroad.com/%s/%s" % (tenant, portal)
    out, seen, page, pages = [], set(), 1, 1
    while page <= pages and page <= 80:
        html = _get(base, params={"page": page})
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", html)
        pages = int(m.group(1)) if m else 1
        for jid, title, loc in SILKROAD_RE.findall(html):
            if jid in seen:
                continue
            seen.add(jid)
            title = strip_html(title)
            if wanted(title):
                out.append(job(org, title, strip_html(loc), "%s/jobs/%s" % (base, jid), None, "silkroad"))
        page += 1
    _seen(org, len(seen))
    return out


AVATURE_RE = re.compile(r'article__header__text__title[^>]*>\s*<a[^>]*href="([^"]*/JobDetail/[^"]*?/(\d+))"[^>]*>(.*?)</a>', re.S)


def fetch_avature(org, base, portal="careers"):
    """Avature career portals: server-rendered, page size fixed at 6."""
    out, seen, offset = [], set(), 0
    while offset < 600:
        html = _get("%s/en_US/%s/SearchJobs/" % (base, portal),
                    params={"jobRecordsPerPage": 6, "jobOffset": offset})
        new = 0
        for href, jid, title in AVATURE_RE.findall(html):
            if jid in seen:
                continue
            seen.add(jid)
            new += 1
            title = strip_html(title)
            if wanted(title):
                out.append(job(org, title, "", href, None, "avature"))
        if not new:
            break
        offset += 6
    _seen(org, len(seen))
    return out


NEOGOV_ROW_RE = re.compile(r'<a[^>]*class="item-details-link"[^>]*data-department-name="([^"]*)"[^>]*'
                           r'href="(/careers/[^/]+/jobs/(\d+)/[^"]*)"[^>]*>(.*?)</a>', re.S)


def _neogov_html(org, agency, dept_re=None):
    """The board's own AJAX list (HTML fragment, each job twice, pages overlap)."""
    seen, out, page = set(), [], 1
    while page <= 40:
        r = requests.get("https://www.governmentjobs.com/careers/home/index", timeout=TIMEOUT,
                         params={"agency": agency, "sort": "PostingDate",
                                 "isDescendingSort": "true", "page": page},
                         headers=dict(HEADERS, **{"X-Requested-With": "XMLHttpRequest"}))
        r.raise_for_status()
        html = r.text
        new = 0
        for dept, href, jid, title in NEOGOV_ROW_RE.findall(html):
            if jid in seen:
                continue
            seen.add(jid)
            new += 1
            dept, title = htmllib.unescape(dept), strip_html(title)
            if not wanted(title) or (dept_re and not re.search(dept_re, dept, re.I)):
                continue
            j = job(org + (" - " + dept if dept else ""), title, "", "https://www.governmentjobs.com" + href,
                    None, "neogov")
            j["sector"] = "Aging Services" if AGING_ORG_RE.search(dept) else "Government & Public Sector"
            out.append(j)
        if not new:
            break
        page += 1
    _seen(org, len(seen))
    return out


BONUS_RE = re.compile(r"\s+[-–—]\s+\$.*$")  # "Case Manager - $2,500 Hiring Bonus!"


def fetch_static_page(org, url, start, end, item_re, location):
    """Agencies that just list openings on a web page, no ATS. Titles are
    the item_re matches between two text markers; the link is the page.
    A missing marker raises, so a redesign shows up in source health
    instead of quietly returning nothing."""
    page = _get(url)
    i = page.find(start)
    if i < 0:
        raise RuntimeError("marker %r not found: page redesigned?" % start)
    j = page.find(end, i + len(start))
    chunk = page[i:j] if j > 0 else page[i:]
    items = [strip_html(m) for m in re.findall(item_re, chunk, re.S)]
    items = [t for t in items if t and not set(t) <= set("_ ")
             and not re.search(r"not accepting|^click here", t, re.I)]
    _seen(org, len(items))
    out = []
    for t in items:
        t = BONUS_RE.sub("", t).strip().rstrip(":")
        if t.isupper():
            t = t.title()
        if wanted(t):
            out.append(job(org, t, location, url, None, "html"))
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
        _seen(org, len(items))
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
    # small WordPress sites paste Word text: cp1252 dashes in "UTF-8" pages
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        return r.content.decode("cp1252", "replace")


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
    ("Workhuman", fetch_ashby, ("Workhuman",)),  # left Workday for Ashby by 2026-07
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
    ("Element Care PACE", fetch_adp, ("52532d91-0809-4e90-8901-6cf15157199d",), "Aging Services"),
    ("NCOA (National Council on Aging)", fetch_adp, ("8efa0a0b-5556-4fc4-83b9-32d6d0dbab79",), "Aging Services"),
    # --- Massachusetts ASAPs (Aging Services Access Points), all 23 per mass.gov 2026-10 ---
    ("SCES (Somerville-Cambridge Elder Services)", fetch_paylocity, ("abc978a8-24e2-4814-8113-155839d73524",), "Aging Services"),
    ("Aging Services of North Central MA", fetch_paylocity, ("b1a0c443-627f-4cb8-817f-1028c6e194c6",), "Aging Services"),
    ("Central Boston Elder Services", fetch_adp, ("0fc1bbc5-ca05-4889-8a06-00eab42270c7",), "Aging Services"),
    ("Elder Services of Cape Cod & the Islands", fetch_adp, ("544f8a58-8b5a-42eb-b8a6-fee3dc7bd35d",), "Aging Services"),
    ("LifePath", fetch_adp, ("ae9f59ae-e029-4ee3-8a2b-afb5e44c27a5",), "Aging Services"),
    ("Bristol Aging & Wellness", fetch_adp, ("efd2c3b2-b56c-43ac-b267-9486fd8a5497",), "Aging Services"),
    ("AgeSpan", fetch_applicantpro, ("https://agespan.applicantpro.com",), "Aging Services"),
    ("Boston Senior Home Care", fetch_applicantpro, ("https://bshcinfo.applicantpro.com",), "Aging Services"),
    ("Greater Springfield Senior Services", fetch_applicantpro, ("https://gsssi.applicantpro.com",), "Aging Services"),
    ("Mystic Valley Aging Services", fetch_applicantpro, ("https://mves.applicantpro.com",), "Aging Services"),
    ("Old Colony Elder Services", fetch_applicantpro, ("https://ocesma.isolvedhire.com",), "Aging Services"),
    ("Greater Lynn Senior Services", fetch_applicantpro, ("https://glss.applicantpool.com",), "Aging Services"),
    ("Springwell", fetch_jazzhr, ("springwell",), "Aging Services"),
    ("Minuteman Senior Services", fetch_paycom, ("3A41F7337B699512CD4289168FC1DB1D",), "Aging Services"),
    ("Tri-Valley Inc.", fetch_rss, ("https://tri-valley-inc.checkwritersrecruit.com/api/rss.xml", "Dudley, MA"), "Aging Services"),
    ("Coastline Elderly Services", fetch_wp_category, ("https://coastlinenb.org", "employment-opportunities", "New Bedford, MA"), "Aging Services"),
    ("HESSCO Elder Services", fetch_static_page, ("https://hessco.org/employment-opportunities/", "CURRENT OPENINGS", "1-800-AGE-INFO", r"<(?:b|strong)>(.*?)</(?:b|strong)>", "Sharon, MA"), "Aging Services"),
    ("Highland Valley Elder Services", fetch_static_page, ("https://highlandvalley.org/about-us/employment-opportunities/", 'id="job-list"', "</section>", r'<h3 class="job-title">([^<]+)<span>', "Florence, MA"), "Aging Services"),
    ("SeniorCare Inc.", fetch_static_page, ("https://seniorcareinc.org/employment/", "CURRENT EMPLOYMENT OPPORTUNITIES", "Our Commitment to Inclusion", r"<h4[^>]*>(.*?)</h4>", "Gloucester, MA"), "Aging Services"),
    ("Elder Services of Worcester Area", fetch_static_page, ("https://eswa.org/about-us/join-us/employment-opportunities/", "Current positions available", "Click here to apply for a position on Indeed", r"<li[^>]*>(.*?)</li>", "Worcester, MA"), "Aging Services"),
    ("Ethos", fetch_static_page, ("https://www.ethocare.org/employment/", "FEATURED POSITIONS", "BENEFITS", r"<p><strong>([^<]+)</strong></p>", "Jamaica Plain, MA"), "Aging Services"),
    ("Access Care Partners", fetch_dejobs, ("Access Care Partners",), "Aging Services"),
    ("Elder Services of Berkshire County", fetch_dejobs, ("Elder Services of Berkshire County",), "Aging Services"),
    # --- other Massachusetts eldercare: PACE, senior housing & living, senior health plans ---
    ("Hebrew SeniorLife", fetch_workday, ("hebrewseniorlife.wd501.myworkdayjobs.com", "hebrewseniorlife", "HebrewSeniorLifeCareers"), "Aging Services"),
    ("2Life Communities", fetch_adp, ("7d58836c-11dd-4415-9de0-63b918b88652",), "Aging Services"),
    ("Rogerson Communities", fetch_ultipro, ("ROG1002RGRC", "92820008-a40a-48bb-830f-94ac1dbdcfa1"), "Aging Services"),
    ("Upham's Elder Service Plan (PACE)", fetch_paycom, ("8D8CDE5BC9EA2D6B2F204BCE42B0BFA7",), "Aging Services"),
    ("Sherrill House", fetch_paycom, ("486E093E988204C1983ED0BE822B1B61",), "Aging Services"),
    ("Harbor Health (Elder Service Plan)", fetch_adp, ("408d9981-05cb-4530-b223-acff48d0efc8",), "Healthcare"),
    ("Fallon Health (Summit ElderCare PACE)", fetch_icims, ("jobs-fchp.icims.com",), "Healthcare"),
    ("Point32Health (Tufts Health Plan)", fetch_workday, ("point32health.wd5.myworkdayjobs.com", "point32health", "THP"), "Healthcare"),
    ("Commonwealth Care Alliance / CareSource", fetch_workday, ("caresource.wd1.myworkdayjobs.com", "caresource", "CareSource"), "Healthcare"),
    ("Volunteers of America Massachusetts", fetch_paylocity, ("832ea9b9-e8a5-4316-8d09-3fa8c6bd8ecd",), "Nonprofit & Civic"),
    ("Brightview Senior Living", fetch_jibe, ("https://careers.brightviewseniorliving.com",), "Aging Services"),
    ("Erickson Senior Living", fetch_workday, ("erickson.wd108.myworkdayjobs.com", "erickson", "External"), "Aging Services"),
    # --- government: one source, one org per hiring agency / department ---
    ("Commonwealth of Massachusetts (MassCareers)", fetch_taleo, ("massanf.taleo.net", "101430233", "ex", "MA state: "), None),
    ("City of Boston", fetch_neogov, ("boston",), None),
    ("NYC Department for the Aging", fetch_nyc_jobs, ("DEPARTMENT FOR THE AGING",), "Aging Services"),
    ("Atlanta Regional Commission", fetch_neogov, ("atlantaregional",), None),
    ("LA County", fetch_neogov, ("lacounty", r"\bAGING\b"), None),
    # --- national aging organizations & Area Agencies on Aging ---
    ("Alzheimer's Association", fetch_ultipro, ("ALZ1000ALZD", "8fe6e004-a2b4-4c6f-b43f-6275765c4462", "recruiting2.ultipro.com"), "Aging Services"),
    ("LeadingAge", fetch_adp, ("b310b93f-df03-41d5-b132-8387d7c58fff",), "Aging Services"),
    ("Council on Aging of Southwestern Ohio (AAA)", fetch_ukg_ready, ("secure7.saashr.com", "6206323"), "Aging Services"),
    ("Detroit Area Agency on Aging", fetch_ukg_ready, ("secure.entertimeonline.com", "DETARE"), "Aging Services"),
    ("Philadelphia Corporation for Aging (AAA)", fetch_paylocity, ("3972b8f8-c245-413b-a5a2-9a3b0a5682b4",), "Aging Services"),
    ("Benjamin Rose Institute on Aging", fetch_paylocity, ("a8916728-1a17-4014-8b05-98827a29d387",), "Aging Services"),
    ("Direction Home Akron Canton (AAA)", fetch_paycom, ("40008A26C629CC3BF8D98B4682ADC9FD",), "Aging Services"),
    ("Homage Senior Services (AAA, Snohomish County)", fetch_paycom, ("9A5555AECDC89B3A7183DDD7974A3CF6",), "Aging Services"),
    ("USAging job board (AAAs nationwide)", fetch_link_list, ("https://www.usaging.org/jobs_search.asp?proc=y", "Job Title", r'<A href="jobs_listing\.asp\?id=\d+">[^<]*</A></td>\s*<td[^>]*><A href="(jobs_listing\.asp\?id=\d+)">(.*?)</A>'), "Aging Services"),
    ("ADvancing States job board (aging & disability)", fetch_link_list, ("https://www.advancingstates.org/index.php/community-opportunities/jobs", "views-field-title", r'views-field-title[^>]*>\s*<strong>\s*<a href="([^"]+)"[^>]*>(.*?)</a>'), "Aging Services"),
    ("Mather", fetch_greenhouse, ("matherheadquarters", "matherintysons", "matherplace", "mathersplendido", "thematherevanston"), "Aging Services"),
    ("Brookdale Senior Living", fetch_oracle_hcm, ("ibmwjb.fa.ocs.oraclecloud.com",), "Aging Services"),
    ("On Lok (PACE)", fetch_oracle_hcm, ("fa-ewag-saasfaprod1.fa.ocs.oraclecloud.com",), "Aging Services"),
    ("Help at Home", fetch_jibe, ("https://operationscareers.helpathome.com",), "Aging Services"),
    ("SCAN Health Plan", fetch_workday, ("scanhealthplan.wd108.myworkdayjobs.com", "scanhealthplan", "scancareers"), "Aging Services"),
    # --- senior health plans & senior care delivery ---
    ("Alignment Health", fetch_workday, ("alignmenthealthcare.wd12.myworkdayjobs.com", "alignmenthealthcare", "ahc_external"), "Healthcare"),
    ("Clover Health", fetch_greenhouse, ("cloverhealth",), "Healthcare"),
    ("Humana / CenterWell", fetch_phenom, ("https://careers.humana.com", "HUMHUMUS", "page24", {"category": ["Technology and Digital Analytics"]}), "Healthcare"),
    ("Oak Street Health (CVS)", fetch_workday, ("cvshealth.wd1.myworkdayjobs.com", "cvshealth", "CVS_Health_Careers", ["Oak Street Health"], {"jobFamilyGroup": ["e65dbadf6a50100168ed7f2a693c0001", "e65dbadf6a50100168ed86fe4cf50001"]}), "Healthcare"),
    ("Trinity Health", fetch_workday, ("trinityhealth.wd1.myworkdayjobs.com", "trinityhealth", "Jobs"), "Healthcare"),
    ("Aledade", fetch_lever, ("aledade",), "Healthcare"),
    ("Papa", fetch_greenhouse, ("papa",), "Tech / Software"),
    ("Arine", fetch_rippling, ("arine",), "Tech / Software"),
    ("Homethrive", fetch_paylocity, ("9eb672aa-b955-4811-bdfa-ea46878abe0f",), "Tech / Software"),
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
    # universities & university-affiliated research (H-1B cap-exempt candidates)
    ("Harvard University", fetch_smartrecruiters, ("HarvardUniversity",), "Academia & Research"),
    ("MIT", fetch_peopleclick, ("client_mit",), "Academia & Research"),
    ("Boston University", fetch_silkroad, ("BU", "External"), "Academia & Research"),
    ("Broad Institute", fetch_avature, ("https://broadinstitute.avature.net",), "Academia & Research"),
    ("Whitehead Institute", fetch_workday, ("wibr.wd1.myworkdayjobs.com", "wibr", "WIBR-Careers"), "Academia & Research"),
    ("UMass Amherst", fetch_rss, ("https://careers.pageuppeople.com/1232/cw/en-us/rss", "Amherst, MA"), "Academia & Research"),
    ("WPI", fetch_workday, ("wpi.wd5.myworkdayjobs.com", "wpi", "WPI_External_Career_Site"), "Academia & Research"),
    ("Brown University", fetch_workday, ("brown.wd5.myworkdayjobs.com", "brown", "staff-careers-brown"), "Academia & Research"),
    ("Yale University", fetch_workday, ("yale.wd1.myworkdayjobs.com", "yale", "external_career_site"), "Academia & Research"),
    ("The Jackson Laboratory", fetch_workday, ("thejacksonlaboratory.wd503.myworkdayjobs.com", "thejacksonlaboratory", "External_JAX"), "Academia & Research"),
    ("ADA Forsyth Institute", fetch_paylocity, ("e7ba7357-ad53-4e69-8b86-906f5c2e3c15",), "Academia & Research"),
    ("UMass Chan Medical School", fetch_jibe, ("https://talent.umassmed.edu",), "Academia & Research"),
    # --- healthcare & academic medicine ---
    ("Mass General Brigham", fetch_workday, ("massgeneralbrigham.wd1.myworkdayjobs.com", "massgeneralbrigham", "MGBExternal"), "Healthcare"),
    ("Beth Israel Lahey Health", fetch_workday, ("bilh.wd1.myworkdayjobs.com", "bilh", "External"), "Healthcare"),
    ("Dana-Farber Cancer Institute", fetch_workday, ("danafarber.wd5.myworkdayjobs.com", "danafarber", "dana-farber"), "Healthcare"),
    ("Boston Medical Center", fetch_workday, ("bmc.wd1.myworkdayjobs.com", "bmc", "BMC"), "Healthcare"),
    ("Boston Children's Hospital", fetch_brassring, ("368", "5205"), "Healthcare"),
    ("UMass Memorial Health", fetch_workday, ("ummh.wd1.myworkdayjobs.com", "ummh", "Careers"), "Healthcare"),
    ("Tufts Medicine", fetch_workday, ("tuftsmedicine.wd1.myworkdayjobs.com", "tuftsmedicine", "Jobs"), "Healthcare"),
    ("NeighborHealth (EBNHC)", fetch_workday, ("ebnhc.wd1.myworkdayjobs.com", "ebnhc", "EBNHC"), "Healthcare"),
    ("Blue Cross Blue Shield of MA", fetch_workday, ("bcbsma.wd5.myworkdayjobs.com", "bcbsma", "BCBSMA"), "Healthcare"),
    # --- nonprofit-serving software ---
    ("Blackbaud", fetch_workday, ("blackbaud.wd1.myworkdayjobs.com", "blackbaud", "ExternalCareers"), "Tech / Software"),
    ("Bonterra", fetch_workday, ("bonterra.wd1.myworkdayjobs.com", "bonterra", "bonterratech"), "Tech / Software"),
]


# ------------------------------------------------------- H-1B cap exemption

BASIS_LABEL = {
    "higher_ed": "Institution of higher education",
    "affiliated_nonprofit": "Nonprofit affiliated with a university",
    "research_org": "Nonprofit or government research organization",
}
CORP_SUFFIX_RE = re.compile(r"\b(INC|INCORPORATED|CORP|CORPORATION|LLC|CO)\.?$")


def _canon(name):
    n = re.sub(r"\(.*?\)", " ", name.upper())
    n = re.sub(r"[^A-Z0-9 ]+", " ", n)
    n = WS_RE.sub(" ", n).strip()
    n = re.sub(r"^THE ", "", n)
    return CORP_SUFFIX_RE.sub("", n).strip()


def cap_exempt_index(orgs):
    """org -> evidence that its employer is ACWIA-covered per DOL prevailing
    wage filings (the cap-exempt categories). Counted only when DOL itself
    issued the ACWIA wage at least once, not on the employer's say-so alone.
    Orgs listed in cap_exempt_orgs.json match by regex; others by exact
    (normalized) name."""
    if not ACWIA.exists():
        return {}, None
    data = json.loads(ACWIA.read_text(encoding="utf-8"))
    emps = data.get("employers", [])
    rules = json.loads(CAP_ORGS.read_text(encoding="utf-8")) if CAP_ORGS.exists() else {}
    names = lambda e: [e["name"]] + e.get("aliases", []) + e.get("dba", [])  # noqa: E731
    by_canon = {}
    for e in emps:
        for n in names(e):
            by_canon.setdefault(_canon(n), []).append(e)
    out = {}
    for org in orgs:
        rule = rules.get(org)
        if isinstance(rule, dict) and rule.get("match"):
            pats = [re.compile(p, re.I) for p in rule["match"]]
            hits = [e for e in emps if any(p.search(n) for p in pats for n in names(e))]
        else:
            hits = by_canon.get(_canon(org), [])
        hits = [e for e in {id(e): e for e in hits}.values() if e.get("dol_acwia_wage")]
        if not hits:
            continue
        hits.sort(key=lambda e: -e["filings"])
        out[org] = {
            "basis": BASIS_LABEL.get(hits[0]["basis"], hits[0]["basis"]),
            "entities": [e["name"] for e in hits[:4]],
            "filings": sum(e["filings"] for e in hits),
            "accepted": sum(e["dol_acwia_wage"] for e in hits),
            "last": max((e.get("last") or "") for e in hits) or None,
            "system": bool(isinstance(rule, dict) and rule.get("system")),
        }
    return out, data.get("source")


def acwia_staleness(sources, today):
    """Days since the newest DOL file's reporting period ended (FY quarters:
    Q1 ends Dec 31 of the prior year, Q4 Sep 30)."""
    ends = []
    for f in sources or []:
        m = re.search(r"FY(\d{4})_Q([1-4])", f)
        if m:
            fy, q = int(m.group(1)), int(m.group(2))
            ends.append({1: "%d-12-31" % (fy - 1), 2: "%d-03-31" % fy,
                         3: "%d-06-30" % fy, 4: "%d-09-30" % fy}[q])
    if not ends:
        return None
    return (datetime.fromisoformat(today) - datetime.fromisoformat(max(ends))).days


EMPTY_ALERT_DAYS = 3  # board answered with no postings at all: moved or renamed
EMPTY_ALERT_MIN_BOARD = 5
FAIL_ALERT_DAYS = 3


def source_health(counts, prev, today):
    """Per-source streaks carried across daily runs. A failing source is
    skipped so the run stays green, and a moved board often answers with an
    empty list instead of an error, so neither shows up unless it is tracked
    over time. Zero *matching* roles is normal for a small agency and only
    recorded (zero_days), never alerted on."""
    health = {}
    for org, (n, total) in counts.items():
        p = prev.get(org) or {}
        h = {"n": n, "total": total, "last_hit": p.get("last_hit"),
             "max_total": max(p.get("max_total") or 0, total or 0),
             "fail_days": 0, "empty_days": 0, "zero_days": 0}
        if n is None:  # failed: keep the other streaks, extend the failure one
            h["fail_days"] = p.get("fail_days", 0) + 1
            h["empty_days"] = p.get("empty_days", 0)
            h["zero_days"] = p.get("zero_days", 0)
        else:
            h["empty_days"] = p.get("empty_days", 0) + 1 if total == 0 else 0
            h["zero_days"] = p.get("zero_days", 0) + 1 if n == 0 else 0
            if n:
                h["last_hit"] = today
        # an empty board is only suspicious if it normally carries postings;
        # a one-job agency between hires is fine (redesigns raise instead)
        h["alert"] = h["fail_days"] >= FAIL_ALERT_DAYS or (
            h["empty_days"] >= EMPTY_ALERT_DAYS and h["max_total"] >= EMPTY_ALERT_MIN_BOARD)
        health[org] = h
    return health


def report_health(health):
    bad = {o: h for o, h in health.items() if h["alert"]}
    for org, h in sorted(bad.items()):
        why = ("failed %d runs in a row" % h["fail_days"] if h["fail_days"]
               else "board returned no postings for %d days" % h["empty_days"])
        # ::warning:: lines become annotations on the GitHub Actions run page
        print("::warning title=Source needs a look::%s - %s" % (org, why))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        rows = ["## Source health", "", "%d sources, %d need a look" % (len(health), len(bad)), ""]
        if bad:
            rows += ["| Source | Board postings today | Failing days | Empty days | Last matching role |",
                     "|---|---|---|---|---|"]
            rows += ["| %s | %s | %d | %d | %s |" % (
                org, "FAILED" if h["n"] is None else h["total"], h["fail_days"],
                h["empty_days"], h["last_hit"] or "never") for org, h in sorted(bad.items())]
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")


FETCH_WORKERS = 8


def fetch_all(specs):
    """Run every source's fetcher concurrently: they are I/O-bound and
    almost all on different hosts, so the daily run takes minutes instead
    of the better part of an hour. Returns {org: jobs or the exception}."""
    names = [org for org, _, _ in specs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit("duplicate source names: %s" % sorted(dupes))

    def run(spec):
        org, fn, args = spec
        try:
            return org, fn(org, *args)
        except Exception as e:  # noqa: BLE001 - reported per source by main()
            return org, e

    with ThreadPoolExecutor(FETCH_WORKERS) as ex:
        return dict(ex.map(run, specs))


def main():
    all_jobs, errors, counts = [], [], {}
    results = fetch_all([(o, f, a) for o, f, a, _ in SOURCES] +
                        [(o, f, a) for o, f, a in ABROAD_SOURCES] +
                        [(o, f, a) for o, f, a, _ in ABROAD_ACADEMIC_SOURCES])
    for org, fn, args, sector in SOURCES:
        try:
            found = results[org]
            if isinstance(found, Exception):
                raise found
            for j in found:
                # multi-agency boards (state / city) set it per job
                j.setdefault("sector", sector or "Government & Public Sector")
            print("  %-34s %3d roles" % (org, len(found)))
            counts[org] = (len(found), BOARD_TOTALS.get(org, len(found)))
            all_jobs.extend(found)
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            print("  %-34s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})
            counts[org] = (None, None)

    for org, fn, args in ABROAD_SOURCES:
        try:
            found = results[org]
            if isinstance(found, Exception):
                raise found
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
            counts[org] = (len(kept), BOARD_TOTALS.get(org, len(found)))
            all_jobs.extend(kept)
        except Exception as e:  # noqa: BLE001
            print("  %-34s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})
            counts[org] = (None, None)

    for org, fn, args, fixed_country in ABROAD_ACADEMIC_SOURCES:
        try:
            found = results[org]
            if isinstance(found, Exception):
                raise found
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
            counts[org] = (len(kept), BOARD_TOTALS.get(org, len(found)))
            all_jobs.extend(kept)
        except Exception as e:  # noqa: BLE001
            print("  %-34s FAILED: %s" % (org, e), file=sys.stderr)
            errors.append({"org": org, "error": str(e)})
            counts[org] = (None, None)

    if MANUAL.exists():
        manual = json.loads(MANUAL.read_text(encoding="utf-8"))
        with ThreadPoolExecutor(8) as ex:
            alive = list(ex.map(manual_alive, manual))
        for m, ok in zip(manual, alive):
            if not ok:
                print("  manual posting gone, skipped: %s | %s" % (m["org"], m["title"]))
        manual = [m for m, ok in zip(manual, alive) if ok]
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

    # cap exemption only means something for US jobs
    cap_idx, cap_src = cap_exempt_index({j["org"] for j in unique if not j.get("abroad")})
    for j in unique:
        if not j.get("abroad") and j["org"] in cap_idx:
            j["cap_exempt"] = True

    # carry over first_seen from the previous run so the dashboard can show
    # "added today"; jobs from before tracking started keep first_seen=null
    prev_seen, had_prev, prev_health = {}, False, {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            for old in prev.get("jobs", []):
                k = (old.get("url") or old.get("org", "")) + "|" + old.get("title", "")
                prev_seen[k] = old.get("first_seen")
            prev_health = prev.get("health") or {}
            had_prev = True
        except Exception:
            pass
    today = datetime.now(timezone.utc).date().isoformat()
    health = source_health(counts, prev_health, today)
    report_health(health)
    cap_age = acwia_staleness(cap_src, today)
    if cap_src is None:
        print("::warning title=Cap-exempt data missing::data/acwia_employers.json not found; "
              "run tools/build_acwia.py on the DOL prevailing-wage files")
    elif cap_age is not None and cap_age > 270:
        print("::warning title=Cap-exempt data is old::newest DOL period ended %d days ago; "
              "download the latest PW_Disclosure_Data xlsx and rerun tools/build_acwia.py" % cap_age)
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
        "health": health,
        "cap_exempt": cap_idx,
        "cap_exempt_source": {"files": cap_src, "age_days": cap_age},
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
