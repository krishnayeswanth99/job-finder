#!/usr/bin/env python3
"""
Job alert bot v2: Ashby + Greenhouse + Lever + discovery + scoring.

Architecture:
  GitHub Action (cron */10)
    -> Sources: Ashby, Greenhouse, Lever, Arbeitnow, Remotive, LinkedIn guest
    -> Normalizer (uniform job dict)
    -> Scoring engine (title/company/location heuristics)
    -> Deduper (seen_jobs.json)
    -> Telegram (score-ranked, HTML-escaped)
    -> companies.json (auto-discovered orgs grow over time)

Files in repo root:
  job_alert.py        this script
  companies.json      {"priority_companies": [...], "known_companies": [...],
                       "known_boards": {...}, "known_ashby_orgs": [...]}
  seen_jobs.json      list of URLs already notified
  .github/workflows/job-alerts.yml
"""

import json
import os
import re
import time
import html
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------- config ---

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = os.environ.get("STATE_FILE", "seen_jobs.json")
COMPANIES_FILE = os.environ.get("COMPANIES_FILE", "companies.json")

# Scoring weights (tweak freely)
W = {
    "new_grad": 5, "swe": 3, "ml": 4, "ai": 3,
    "priority_company": 5, "known_company": 1,
    "remote": 1, "visa_flag": 2,
}
MIN_SCORE = 6          # don't notify below this
MAX_AGE_HOURS = 72

# Static seed lists -- discovery grows these automatically
DEFAULT_COMPANIES = {
    "priority_companies": [
        "openai", "anthropic", "databricks", "stripe", "palantir",
        "cohere", "perplexity", "anyscale", "scale ai", "mercor",
    ],
    "known_companies": [],          # filled by discovery
    "known_boards": {               # slug -> "greenhouse"|"lever"|"ashby"
        "openai": "greenhouse", "anthropic": "greenhouse",
        "databricks": "greenhouse", "stripe": "greenhouse",
        "palantir": "greenhouse", "airbnb": "greenhouse",
        "doordash": "greenhouse", "robinhood": "greenhouse",
        "scaleai": "greenhouse", "cohere": "greenhouse",
        "perplexityai": "greenhouse", "anyscale": "greenhouse",
        "instacart": "greenhouse", "okta": "greenhouse",
        "mongodb": "greenhouse", "duolingo": "lever",
        "anduril": "lever", "hopper": "lever",
        "spotify": "lever", "netlify": "lever",
        "elevenlabs": "ashby", "mercor": "ashby",
        "clay": "ashby", "cursor": "ashby",
    },
    "known_ashby_orgs": ["elevenlabs", "mercor", "clay", "cursor"],
}

ARBEITNOW_PAGES = 2
REMotive_SEARCHES = ["machine learning", "software engineer"]
LINKEDIN_QUERIES = [
    ("new grad software engineer 2027", "United States"),
    ("machine learning engineer new grad", "United States"),
    ("AI engineer new grad", "United States"),
]

UA = {"User-Agent": "Mozilla/5.0 (job-alert-bot; personal use)"}
TAG_RE = re.compile(r"<[^>]+>")


# ------------------------------------------------------------- utilities ---

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8", "replace"))


def fresh(ts):
    if not ts or MAX_AGE_HOURS is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str):
            v = ts.strip()
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        else:
            return True
        return dt > cutoff
    except (ValueError, TypeError, OverflowError):
        return True


# ------------------------------------------------------- companies state ---

def load_companies():
    if os.path.exists(COMPANIES_FILE):
        with open(COMPANIES_FILE) as f:
            data = json.load(f)
        # merge with defaults so new seed boards appear on upgrade
        for k, v in DEFAULT_COMPANIES.items():
            if k not in data:
                data[k] = v
        return data
    return json.loads(json.dumps(DEFAULT_COMPANIES))  # deep copy


def save_companies(c):
    with open(COMPANIES_FILE, "w") as f:
        json.dump(c, f, indent=1, sort_keys=True)


def discover_company(c, name, board_type=None, slug=None):
    """Auto-add any company we encounter. Returns True if newly added."""
    key = (name or "").strip().lower()
    if not key:
        return False
    added = False
    if key not in c["known_companies"]:
        c["known_companies"].append(key)
        added = True
    if board_type and slug and slug not in c["known_boards"]:
        c["known_boards"][slug] = board_type
        if board_type == "ashby" and slug not in c["known_ashby_orgs"]:
            c["known_ashby_orgs"].append(slug)
        added = True
    return added


# ------------------------------------------------------------- scoring -----

def score_job(job, companies):
    title = job["title"].lower()
    loc = (job.get("location") or "").lower()
    comp = (job.get("company") or "").lower()
    s = 0
    tags = []

    if any(k in title for k in ["new grad", "graduate", "university", "entry level"]):
        s += W["new_grad"]; tags.append("new-grad")
    if "software engineer" in title or "swe" in title:
        s += W["swe"]; tags.append("swe")
    if any(k in title for k in ["machine learning", "ml engineer", "deep learning"]):
        s += W["ml"]; tags.append("ml")
    if any(k in title for k in ["ai engineer", "artificial intelligence", "llm", "genai"]):
        s += W["ai"]; tags.append("ai")
    if comp in [c.lower() for c in companies["priority_companies"]]:
        s += W["priority_company"]; tags.append("⭐priority")
    elif comp in companies["known_companies"]:
        s += W["known_company"]
    if "remote" in loc or "anywhere" in loc:
        s += W["remote"]; tags.append("remote")
    if "visa" in (job.get("src") or ""):
        s += W["visa_flag"]; tags.append("visa")

    job["score"] = s
    job["tags"] = tags
    return s


# ---------------------------------------------------------------- sources ---

def greenhouse_jobs(slug):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    for job in data.get("jobs", []):
        yield {
            "title": job["title"], "company": slug,
            "location": (job.get("location") or {}).get("name", ""),
            "url": job["absolute_url"],
            "posted_ts": job.get("updated_at"),
            "src": "greenhouse", "board_slug": slug, "board_type": "greenhouse",
        }


def lever_jobs(slug):
    for job in fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        yield {
            "title": job["text"], "company": slug,
            "location": (job.get("categories") or {}).get("location", ""),
            "url": job["hostedUrl"],
            "posted_ts": (job.get("createdAt") or 0) / 1000 or None,
            "src": "lever", "board_slug": slug, "board_type": "lever",
        }


def ashby_jobs(org):
    """Ashby public API: api.ashbyhq.com/posting-api/job-board/<org>"""
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    for job in data.get("jobs", []):
        loc = job.get("location") or ""
        yield {
            "title": job.get("title", ""),
            "company": org,
            "location": loc if isinstance(loc, str) else loc.get("name", ""),
            "url": job.get("jobUrl", f"https://jobs.ashbyhq.com/{org}/{job.get('id','')}"),
            "posted_ts": job.get("publishedAt"),
            "src": "ashby", "board_slug": org, "board_type": "ashby",
        }


def arbeitnow_jobs():
    for page in range(1, ARBEITNOW_PAGES + 1):
        for job in fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}").get("results", []):
            yield {
                "title": job["title"], "company": job.get("company_name", ""),
                "location": job.get("location", "") or ("Remote" if job.get("remote") else ""),
                "url": job["url"], "posted_ts": job.get("created_at"),
                "src": "arbeitnow" + (" [visa-sponsorship]" if job.get("visa_sponsorship") else ""),
            }


def remotive_jobs(search):
    q = urllib.parse.quote(search)
    for job in fetch_json(f"https://remotive.com/api/remote-jobs?search={q}&limit=50").get("jobs", []):
        yield {
            "title": TAG_RE.sub("", job["title"]), "company": job.get("company_name", ""),
            "location": "Remote", "url": job["url"],
            "posted_ts": job.get("publication_date") and
                         time.mktime(time.strptime(job["publication_date"], "%Y-%m-%dT%H:%M:%S")),
            "src": "remotive",
        }


def linkedin_jobs(keywords, location):
    q, loc = urllib.parse.quote(keywords), urllib.parse.quote(location)
    url = (f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings"
           f"/search?keywords={q}&location={loc}&start=0")
    try:
        h = fetch(url).decode("utf-8", "replace")
    except Exception as e:
        print(f"[linkedin] {keywords}: {e}")
        return
    ids = re.findall(r'/jobs/view/(\d+)', h)
    titles = re.findall(r'base-search-card__title[^>]*>\s*(.+?)\s*<', h, re.S)
    comps = re.findall(r'base-search-card__subtitle[^>]*>\s*(.+?)\s*<', h, re.S)
    seen = set()
    for i, jid in enumerate(ids):
        if jid in seen or i >= len(titles):
            continue
        seen.add(jid)
        yield {
            "title": TAG_RE.sub("", titles[i]).strip(),
            "company": TAG_RE.sub("", comps[i]).strip() if i < len(comps) else "",
            "location": location, "url": f"https://www.linkedin.com/jobs/view/{jid}",
            "posted_ts": None, "src": "linkedin",
        }


# ------------------------------------------------------------- telegram ---

def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[telegram_api_error] {e.code}: {e.read().decode('utf-8','replace')}")
        raise


def notify(jobs):
    for j in sorted(jobs, key=lambda x: -x["score"]):
        msg = (f"🔥 <b>Score: {j['score']}</b>  {' '.join('#'+t for t in j['tags'])}\n"
               f"<b>{html.escape(j['title'])}</b>\n"
               f"🏢 {html.escape(str(j['company']))}\n"
               f"📍 {html.escape(str(j.get('location') or 'N/A'))}\n"
               f"🔗 {j['url']}\n"
               f"<i>{html.escape(str(j['src']))}</i>")
        try:
            telegram_send(msg)
        except Exception as e:
            print(f"[telegram] failed for {j['url']}: {e}")
        time.sleep(0.34)


# ------------------------------------------------------------------ main ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=0)


def main():
    companies = load_companies()
    seen = load_state()
    all_jobs = []

    # 1) Poll every known board (greenhouse/lever/ashby from companies.json)
    for slug, btype in list(companies["known_boards"].items()):
        fn = {"greenhouse": greenhouse_jobs, "lever": lever_jobs, "ashby": ashby_jobs}.get(btype)
        if not fn:
            continue
        try:
            all_jobs.extend(fn(slug))
        except Exception as e:
            print(f"[{btype}] {slug}: {e}")

    # 2) Discovery sources
    for fn, label in [(arbeitnow_jobs, "arbeitnow")]:
        try:
            all_jobs.extend(fn())
        except Exception as e:
            print(f"[{label}]: {e}")
    for s in REMotive_SEARCHES:
        try:
            all_jobs.extend(remotive_jobs(s))
        except Exception as e:
            print(f"[remotive]: {e}")
    for k, l in LINKEDIN_QUERIES:
        all_jobs.extend(linkedin_jobs(k, l))

    # 3) Auto-discover companies from everything we saw
    for j in all_jobs:
        discover_company(companies, j.get("company"),
                         j.get("board_type"), j.get("board_slug"))

    # 4) Score + filter
    for j in all_jobs:
        score_job(j, companies)
    matched = [j for j in all_jobs
               if j["score"] >= MIN_SCORE and fresh(j.get("posted_ts"))]
    new_jobs = [j for j in matched if j["url"] not in seen]

    print(f"scanned={len(all_jobs)} matched={len(matched)} new={len(new_jobs)} "
          f"companies_known={len(companies['known_companies'])}")

    if new_jobs:
        notify(new_jobs)
        seen.update(j["url"] for j in new_jobs)

    save_state(seen)
    save_companies(companies)


if __name__ == "__main__":
    main()