#!/usr/bin/env python3
"""
Free job-alert bot: polls public job-board APIs, filters for target roles,
dedupes, and sends instant Telegram notifications.

Sources (all free, no API keys):
  - Greenhouse boards  (boards-api.greenhouse.io)   -- most startups + big tech
  - Lever postings     (api.lever.co)                -- startups
  - Arbeitnow          (arbeitnow.com/api)           -- has visa_sponsorship flag!
  - Remotive           (remotive.com/api)            -- remote jobs
  - LinkedIn guest API (jobs-guest endpoint)         -- unofficial, no login needed

State (seen_jobs.json) is committed back to the repo by the workflow,
so the bot never double-notifies.

Setup: see README steps in the chat / workflow file.
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------- config ---

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = os.environ.get("STATE_FILE", "seen_jobs.json")

# Only notify if the job title contains at least one of these:
TITLE_KEYWORDS = [
    "new grad", "graduate", "university", "entry level",
    "software engineer", "machine learning", "ml engineer", "deep learning",
    "ai engineer", "artificial intelligence", "applied scientist",
    "data scientist", "backend engineer", "research engineer",
]

# ...AND the location field contains at least one of these (set to [] to disable)
LOCATION_KEYWORDS = [
    "united states", "usa", "u.s.", "new york", "san francisco",
    "seattle", "bay area", "remote", "anywhere",
]

# Don't notify about jobs posted longer ago than this (None = no age filter)
MAX_AGE_HOURS = 72

# Company boards to watch. Slugs = the subdomain of their careers page:
# boards.greenhouse.io/<slug>  or  jobs.lever.co/<slug>
GREENHOUSE_BOARDS = [
    "openai", "anthropic", "databricks", "stripe", "palantir",
    "airbnb", "doordash", "robinhood", "scaleai", "cohere",
    "perplexityai", "anyscale", "instacart", "okta", "mongodb",
]
LEVER_BOARDS = [
    "netlify", "duolingo", "anduril", "hopper", "spotify",
]
ARBEITNOW_PAGES = 2          # pages to scan (20 jobs/page)
REMotive_SEARCHES = ["machine learning", "software engineer"]

LINKEDIN_QUERIES = [
    ("new grad software engineer 2027", "United States"),
    ("machine learning engineer new grad", "United States"),
    ("AI engineer new grad", "United States"),
]

UA = {"User-Agent": "Mozilla/5.0 (job-alert-bot; personal use)"}


# ------------------------------------------------------------- utilities ---

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8", "replace"))


def title_matches(title):
    t = title.lower()
    return any(k in t for k in TITLE_KEYWORDS)


def location_matches(loc):
    if not LOCATION_KEYWORDS:
        return True
    l = (loc or "").lower()
    return any(k in l for k in LOCATION_KEYWORDS)


def fresh(timestamp):
    """
    Accept Unix timestamps, ISO-8601 strings, or missing timestamps.
    Returns True if the job is within MAX_AGE_HOURS.
    """
    if not timestamp or MAX_AGE_HOURS is None:
        return True

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    try:
        # Unix timestamp
        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        # ISO-8601 timestamp
        elif isinstance(timestamp, str):
            value = timestamp.strip()

            # Greenhouse commonly returns:
            # 2026-09-16T12:34:56-0400
            # or 2026-09-16T16:34:56Z
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"

            dt = datetime.fromisoformat(value)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

        else:
            return True

        return dt > cutoff

    except (ValueError, TypeError, OverflowError) as e:
        print(f"[timestamp] Could not parse {timestamp!r}: {e}")
        return True


def collect(jobs):
    """Apply title/location/age filters."""
    out = []
    for j in jobs:
        if title_matches(j["title"]) and location_matches(j.get("location")) \
                and fresh(j.get("posted_ts")):
            out.append(j)
    return out


# ---------------------------------------------------------------- sources ---

def greenhouse_jobs(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = fetch_json(url)
    for job in data.get("jobs", []):
        yield {
            "title": job["title"],
            "company": slug,
            "location": (job.get("location") or {}).get("name", ""),
            "url": job["absolute_url"],
            "posted_ts": job.get("updated_at"),
            "src": "greenhouse",
        }


def lever_jobs(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    for job in fetch_json(url):
        yield {
            "title": job["text"],
            "company": slug,
            "location": (job.get("categories") or {}).get("location", ""),
            "url": job["hostedUrl"],
            "posted_ts": (job.get("createdAt") or 0) / 1000 or None,
            "src": "lever",
        }


def arbeitnow_jobs():
    for page in range(1, ARBEITNOW_PAGES + 1):
        url = f"https://www.arbeitnow.com/api/job-board-api?page={page}"
        for job in fetch_json(url).get("results", []):
            yield {
                "title": job["title"],
                "company": job.get("company_name", ""),
                "location": job.get("location", "") or ("Remote" if job.get("remote") else ""),
                "url": job["url"],
                "posted_ts": job.get("created_at"),
                "src": "arbeitnow" + (" [visa-sponsorship]" if job.get("visa_sponsorship") else ""),
            }


def remotive_jobs(search):
    q = urllib.parse.quote(search)
    url = f"https://remotive.com/api/remote-jobs?search={q}&limit=50"
    for job in fetch_json(url).get("jobs", []):
        yield {
            "title": re.sub(r"<[^>]+>", "", job["title"]),
            "company": job.get("company_name", ""),
            "location": "Remote",
            "url": job["url"],
            "posted_ts": job.get("publication_date") and
                         time.mktime(time.strptime(job["publication_date"], "%Y-%m-%dT%H:%M:%S")),
            "src": "remotive",
        }


TAG_RE = re.compile(r"<[^>]+>")

def linkedin_jobs(keywords, location):
    """Unofficial guest endpoint: no login, no cookies. HTML scraping --
    brittle by nature; failures are non-fatal."""
    q = urllib.parse.quote(keywords)
    loc = urllib.parse.quote(location)
    url = (f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings"
           f"/search?keywords={q}&location={loc}&start=0")
    try:
        html = fetch(url).decode("utf-8", "replace")
    except Exception as e:
        print(f"[linkedin] {keywords}: {e}")
        return
    ids = re.findall(r'/jobs/view/(\d+)', html)
    titles = re.findall(r'base-search-card__title[^>]*>\s*(.+?)\s*<', html, re.S)
    comps = re.findall(r'base-search-card__subtitle[^>]*>\s*(.+?)\s*<', html, re.S)
    seen = set()
    for i, jid in enumerate(ids):
        if jid in seen or i >= len(titles):
            continue
        seen.add(jid)
        yield {
            "title": TAG_RE.sub("", titles[i]).strip(),
            "company": TAG_RE.sub("", comps[i]).strip() if i < len(comps) else "",
            "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{jid}",
            "posted_ts": None,
            "src": "linkedin",
        }


# ------------------------------------------------------------- telegram ---

def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def notify(jobs):
    for j in jobs:
        msg = (f"🚨 <b>{j['title']}</b>\n"
               f"🏢 {j['company']}\n"
               f"📍 {j.get('location') or 'N/A'}\n"
               f"🔗 {j['url']}\n"
               f"<i>{j['src']}</i>")
        try:
            telegram_send(msg)
        except Exception as e:
            print(f"[telegram] failed for {j['url']}: {e}")
        time.sleep(0.34)  # stay well under Telegram rate limits


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
    seen = load_state()
    all_jobs = []

    tasks = ([(greenhouse_jobs, s) for s in GREENHOUSE_BOARDS] +
             [(lever_jobs, s) for s in LEVER_BOARDS])
    for fn, slug in tasks:
        try:
            all_jobs.extend(fn(slug))
        except Exception as e:
            print(f"[{fn.__name__}] {slug}: {e}")   # 404 slugs etc. -- skip

    try:
        all_jobs.extend(arbeitnow_jobs())
    except Exception as e:
        print(f"[arbeitnow]: {e}")

    for s in REMotive_SEARCHES:
        try:
            all_jobs.extend(remotive_jobs(s))
        except Exception as e:
            print(f"[remotive]: {e}")

    for k, l in LINKEDIN_QUERIES:      # linkedin_jobs handles its own errors
        all_jobs.extend(linkedin_jobs(k, l))

    matched = collect(all_jobs)
    new_jobs = [j for j in matched if j["url"] not in seen]

    print(f"scanned={len(all_jobs)} matched={len(matched)} new={len(new_jobs)}")
    if new_jobs:
        notify(new_jobs)
        seen.update(j["url"] for j in new_jobs)
    save_state(seen)


if __name__ == "__main__":
    main()
