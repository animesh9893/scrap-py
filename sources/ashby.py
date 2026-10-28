"""Ashby, the applicant tracking system many startups host their careers page on (jobs.ashbyhq.com/<board>).

Unlike LinkedIn this isn't a keyword × location search: each company has one public job board, and Ashby's posting
API returns every job on it in one request, with the full description, no login and no scraping:

    GET https://api.ashbyhq.com/posting-api/job-board/<board>?includeCompensation=true

boards.py reads the boards of the companies in the companies table (companies.ats = 'ashby', from
data/ats-companies.csv) and saves their jobs with source 'ashby'. This module only fetches a board and turns its jobs
into rows shaped like public.jobs (see sources/__init__.py). The site is for tech jobs, so a board's other jobs
(sales, legal, operations…) and jobs published more than `max_age_days` ago are left out.
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import experience
from search import is_non_tech

NAME = "ashby"
LABEL = "Ashby"
API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
BOARD_URL = re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)", re.I)
TIMEOUT = 60      # seconds; the biggest boards list thousands of jobs

EMPLOYMENT = {"FullTime": "Full-time", "PartTime": "Part-time", "Intern": "Internship", "Contract": "Contract",
              "Temporary": "Temporary"}
WORKPLACE = {"Remote": "Remote", "Hybrid": "Hybrid", "OnSite": "On-site"}

# A software / tech title: about the words the LinkedIn posts' filter (post_job_ok in base.sql) accepts.
TECH = re.compile(r"(engineer|developer|\bsde\b|programmer|software|scientist|data analyst|analytics|devops|\bsre\b|"
                  r"full ?stack|front ?end|back ?end|android|\bios\b|mobile|machine learning|\bml\b|\bai\b|\bllm|cloud|"
                  r"security|\bqa\b|\btest|automation|infrastructure|architect|\bdata\b)", re.I)
# Engineering words that aren't software here.
NOT_SOFTWARE = re.compile(r"\b(sales engineer|solutions? engineer|support engineer|field engineer|"
                          r"(customer|deployment|forward deployed|implementation|onboarding) engineer|"
                          r"recruit\w*|account executive|legal|counsel|paralegal)\b", re.I)


class NotFound(Exception):
    """The board doesn't exist (any more): Ashby answers 404."""


def board_slug(url):
    """'https://jobs.ashbyhq.com/openai' -> 'openai'; None for anything else."""
    match = BOARD_URL.match((url or "").strip())
    return match[1] if match else None


def is_tech(title):
    return bool(TECH.search(title or "")) and not NOT_SOFTWARE.search(title or "") and not is_non_tech(title)


def fetch(session, board):
    """The board's JSON ({"jobs": [...]}). Raises NotFound for a board that doesn't exist, and requests' errors."""
    r = session.get(API.format(board=board), timeout=TIMEOUT, headers={"Accept": "application/json"})
    if r.status_code == 404:
        raise NotFound(board)
    r.raise_for_status()
    return r.json()


def parse_time(text):
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def salary(job):
    comp = job.get("compensation") or {}
    if job.get("shouldDisplayCompensationOnJobPostings") is False:
        return None
    return comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or None


def location(job):
    """'San Francisco, California, United States', plus '+2 more' for jobs open in several places."""
    main = (job.get("location") or "").strip()
    more = [s.get("location") for s in job.get("secondaryLocations") or [] if s.get("location")]
    return f"{main} +{len(more)} more" if main and more else main or (more[0] if more else None)


def to_row(job, company, board, now):
    """One Ashby job as a public.jobs row."""
    address = ((job.get("address") or {}).get("postalAddress") or {})
    workplace = "Remote" if job.get("isRemote") else WORKPLACE.get(job.get("workplaceType"))
    description = (job.get("descriptionPlain") or "").strip() or None
    return {
        "source": NAME,
        "job_id": job["id"],
        "title": (job.get("title") or "").strip(),
        "company": company,
        "company_url": f"https://jobs.ashbyhq.com/{board}",
        "company_slug": None,
        "location": location(job),
        "workplace": workplace,
        "url": job.get("jobUrl"),
        "apply_url": job.get("applyUrl"),
        "posted_at": job.get("publishedAt"),
        "employment_type": EMPLOYMENT.get(job.get("employmentType"), job.get("employmentType")),
        "seniority": None,
        "salary": salary(job),
        "description": description,
        "search_name": f"ashby:{board}",
        "raw": {
            "board": board,
            "department": job.get("department"),
            "team": job.get("team"),
            "country": address.get("addressCountry"),
            "secondary_locations": [s.get("location") for s in job.get("secondaryLocations") or []],
            "experience_years": experience.years(job.get("title"), "", description or ""),
            "experience_parser": experience.VERSION,
        },
        "last_seen_at": now.isoformat(),
    }


def rows(data, company, board, now=None, max_age_days=30):
    """The tech jobs on a board published in the last `max_age_days` days, as public.jobs rows, and counts:
    {"listed", "kept", "skipped": {"unlisted", "not_tech", "too_old"}}."""
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(days=max_age_days) if max_age_days else None
    kept, skipped = [], Counter()
    jobs = data.get("jobs") or []
    for job in jobs:
        if not job.get("id") or job.get("isListed") is False:
            skipped["unlisted"] += 1
        elif not is_tech(job.get("title")):
            skipped["not_tech"] += 1
        elif oldest and (parse_time(job.get("publishedAt")) or now) < oldest:
            skipped["too_old"] += 1
        else:
            kept.append(to_row(job, company, board, now))
    return kept, {"listed": len(jobs), "kept": len(kept), "skipped": dict(skipped)}
