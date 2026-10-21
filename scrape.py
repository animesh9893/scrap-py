"""Krawlr LinkedIn job scraper, built on JobSpy's LinkedIn scraper (python-jobspy, pinned in requirements.txt).

Runs on GitHub Actions (.github/workflows/scrape.yml) and writes to OUT_DIR (default out/), which the workflow
uploads as the run's artifact:
  jobs.json    {"meta": search, filters, counts, errors; "jobs": [rows]}: one row per job, shaped like the
               public.jobs table (see ROW_FIELDS), ready to upsert on (source, job_id)
  jobs.csv     the same rows, raw as a JSON string, for Excel / Sheets / COPY
  jobs.md      a Markdown table of the jobs (also shown on the run page)
  report.html  a self-contained page: search box, one card per job, expandable descriptions

Filters. LinkedIn's logged-out search only applies some of them, so the rest are checked on each job's own page:
  by LinkedIn   SEARCH_TERM, LOCATION, DISTANCE, HOURS_OLD, COMPANY_IDS, EASY_APPLY
  by us         JOB_TYPE, EXPERIENCE (from the job page), EXCLUDE (words in the title), HOURS_OLD (re-checked to the
                hour from "5 hours ago"), SORT (LinkedIn ignores sortBy; "recent" sorts the jobs kept, newest first)
LinkedIn ignores its remote / hybrid / on-site filter for logged-out searches and doesn't show the workplace type
on the job page, so there is no remote filter; put "remote" in SEARCH_TERM instead.

Every setting can be overridden with an environment variable of the same name (the workflow's inputs set them).
Lists are comma-separated.
"""

import csv
import html
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.util import criteria
from jobspy.model import DescriptionFormat, ScraperInput
from jobspy.util import check_status, description_fields, get_enum_from_job_type


def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """KEY=value lines from .env (next to this script) into the environment, for local runs. Variables already set
    win, so the workflow's inputs aren't overridden. There's no .env on GitHub Actions (it's gitignored)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()


def env(name, default):
    value = (os.environ.get(name) or "").strip()
    return value if value else default


def env_list(name, default):
    value = env(name, None)
    items = [x.strip() for x in value.split(",") if x.strip()] if value else default
    return [x for x in items if x.lower() != "any"]      # "any" is the workflow's "no filter" choice


def env_bool(name, default):
    value = env(name, None)
    return value.lower() in ("1", "true", "yes", "y") if value else default


def env_int(name, default):
    value = env(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {value!r}")


# ---------------------------- CONFIG ----------------------------
SEARCH_TERM  = env("SEARCH_TERM", "software engineer")
LOCATION     = env("LOCATION", "Bengaluru")
SEARCH_NAME  = env("SEARCH_NAME", f"{SEARCH_TERM} in {LOCATION}")   # stored in each row's search_name
DISTANCE     = env_int("DISTANCE", 50)          # miles around LOCATION
HOURS_OLD    = env_int("HOURS_OLD", 24)         # only jobs posted in the last N hours; 0 = any time
RESULTS      = env_int("RESULTS", 30)           # jobs to keep, after filtering
SORT         = env("SORT", "recent").lower()    # recent (newest first) | relevant (LinkedIn's order)
COMPANY_IDS  = env_list("COMPANY_IDS", [])      # LinkedIn company ids (f_C), numbers
EASY_APPLY   = env_bool("EASY_APPLY", False)

JOB_TYPE     = env_list("JOB_TYPE", [])         # full-time, part-time, contract, temporary, internship, volunteer
EXPERIENCE   = env_list("EXPERIENCE", [])       # internship, entry level, associate, mid-senior level, director, executive
EXCLUDE      = env_list("EXCLUDE", [])          # drop jobs with any of these words in the title

SCAN_LIMIT   = env_int("SCAN_LIMIT", RESULTS * 5)   # stop after looking at this many jobs (each is one request)
PROXIES      = env_list("PROXIES", [])          # e.g. user:pass@host:port,host2:port (a repo secret)
OUT_DIR      = env("OUT_DIR", "out")
# ----------------------------------------------------------------

log = logging.getLogger(__name__)


def norm(text):
    """Letters only, lower case: "Mid-Senior level" -> "midseniorlevel", "full-time" -> "fulltime"."""
    return re.sub(r"[^a-z]", "", (text or "").lower())


# What LinkedIn's job page shows ("Employment type", "Seniority level"), normalised
JOB_TYPE_VALUES   = ["fulltime", "parttime", "contract", "temporary", "internship", "volunteer", "other"]
EXPERIENCE_VALUES = ["internship", "entrylevel", "associate", "midseniorlevel", "director", "executive"]


def check_settings():
    """Stop with a clear message on a setting that would otherwise filter out every job (or nothing) silently."""
    problems = []
    if SORT not in ("recent", "relevant"):
        problems.append(f"SORT must be recent or relevant, got {SORT!r}")
    for value in JOB_TYPE:
        if norm(value) not in JOB_TYPE_VALUES:
            problems.append(f"JOB_TYPE {value!r} isn't one of: full-time, part-time, contract, temporary, internship, "
                            "volunteer, other")
    for value in EXPERIENCE:
        if not norm(value) or not any(norm(value) in level for level in EXPERIENCE_VALUES):
            problems.append(f"EXPERIENCE {value!r} isn't one of: internship, entry level, associate, mid-senior level, "
                            "director, executive")
    for value in COMPANY_IDS:
        if not value.isdigit():
            problems.append(f"COMPANY_IDS {value!r} isn't a LinkedIn company id (a number, like 1441 for Google)")
    if min(HOURS_OLD, RESULTS, DISTANCE) < 0 or RESULTS == 0:
        problems.append("HOURS_OLD and DISTANCE can't be negative, and RESULTS must be at least 1")
    if problems:
        raise SystemExit("Invalid settings:\n  " + "\n  ".join(problems))


HOURS_PER = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 24 * 7, "month": 24 * 30, "year": 24 * 365}


def age_hours(posted_ago):
    """ "5 hours ago" -> 5, "2 weeks ago" -> 336, "Just now" -> 0; None when unknown."""
    if "just now" in (posted_ago or "").lower():
        return 0
    match = re.match(r"(\d+)\s+(minute|hour|day|week|month|year)", posted_ago or "")
    return int(match[1]) * HOURS_PER[match[2]] if match else None


class Skip(Exception):
    pass


STOP = "scan limit reached"
DETAIL_DELAY = (0.5, 1.5)    # seconds between job pages; back to back, LinkedIn starts answering 429
RETRY_WAITS = (5, 15)        # seconds before each retry of a request LinkedIn rate-limited (429) or failed (5xx)
RETRY_STATUSES = {429, 500, 502, 503, 504, 999}


class RetryingSession:
    """JobSpy's HTTP session, with each GET that LinkedIn rate-limits or fails retried after RETRY_WAITS: search
    pages and job pages alike (JobSpy stops the whole search at the first failed search page)."""

    def __init__(self, session, log):
        self._session, self._log = session, log

    def __getattr__(self, name):
        return getattr(self._session, name)

    def get(self, url, **kwargs):
        for wait in RETRY_WAITS + (None,):
            try:
                response = self._session.get(url, **kwargs)
                if response.status_code not in RETRY_STATUSES or wait is None:
                    return response
                problem = f"status {response.status_code}"
            except Exception as e:
                if wait is None:
                    raise
                problem = str(e)
            self._log.warning(f"{urlparse(url).path}: {problem}, retrying in {wait}s")
            time.sleep(wait)


# ============ 1. Scrape ============
class FilteredLinkedIn(LinkedIn):
    """JobSpy's LinkedIn scraper, plus the filters LinkedIn ignores for logged-out searches and the job-page fields
    JobSpy leaves out.

    JobSpy's search loop skips a job when _process_job raises and keeps paging until it has results_wanted jobs, so
    the filters raise Skip and the run still ends up with RESULTS matching jobs (or stops at SCAN_LIMIT).
    """

    def __init__(self):
        super().__init__()
        self.scanned = 0
        self.hit_scan_limit = False
        self.skipped = Counter()
        self.posted_ago = {}
        self.page_extras = {}      # job id -> what _fetch_details reads beyond JobSpy's fields
        self.oldest = (datetime.now(timezone.utc) - timedelta(hours=HOURS_OLD, days=1)).date() if HOURS_OLD else None
        self.log.addFilter(lambda rec: rec.getMessage() != f"skipping job: {STOP}")   # said once, below

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, value):              # JobSpy's scrape() sets it; every request then goes through the retries
        self._session = RetryingSession(value, self.log)

    def _fetch_details(self, job_id):
        """The job page: JobSpy's fields (description, pay, type, seniority, function, industries, logo), plus the
        applicant count, LinkedIn's pay text and how to apply. Empty when the page can't be read."""
        time.sleep(random.uniform(*DETAIL_DELAY))
        try:
            response = self.session.get(f"{self.base_url}/jobs/view/{job_id}")
            check_status(response)
        except Exception as e:
            self.log.warning(f"job {job_id}: {e}, giving up on its page")
            return {}
        if not urlparse(response.url).path.startswith("/jobs/view/"):
            self.log.warning(f"job {job_id}: redirected to {urlparse(response.url).path}")
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        markup = soup.find("div", class_="show-more-less-html__markup")
        if markup:
            markup.attrs.clear()
        employment_type = criteria(soup, "Employment type")
        job_type = get_enum_from_job_type(employment_type.lower().replace("-", "")) if employment_type else None
        seniority = criteria(soup, "Seniority level")
        salary_tag = soup.find("div", class_="compensation__salary")
        pay = self._parse_salary(salary_tag.get_text() if salary_tag else "")
        logo = soup.find("img", class_="artdeco-entity-image")
        applicants = soup.find(class_="num-applicants__caption")
        controls = {tag["data-tracking-control-name"] for tag in soup.find_all(attrs={"data-tracking-control-name": True})}
        self.page_extras[f"li-{job_id}"] = {
            "salary_text": " ".join(salary_tag.get_text(" ", strip=True).split()) if salary_tag else None,
            "applicants_text": applicants.get_text(" ", strip=True) if applicants else None,
            "apply_type": "easy_apply" if "public_jobs_apply-link-onsite" in controls else
                          "company_site" if any(c.startswith("public_jobs_apply-link-offsite") for c in controls) else None,
        }
        return {
            **description_fields(markup.prettify(formatter="html") if markup else None, DescriptionFormat.HTML),
            **pay.model_dump(),
            "salary_source": "linkedin" if pay.min_amount else None,
            "job_type": [job_type] if job_type else None,
            "job_level": seniority.lower() if seniority else None,
            "job_function": criteria(soup, "Job function"),
            "company_industry": criteria(soup, "Industries"),
            "company_logo": logo.get("data-delayed-url") if logo else None,
        }

    def _skip(self, reason, message):
        self.skipped[reason] += 1
        raise Skip(message)

    def _process_job(self, job_card, job_id):
        if self.scanned >= SCAN_LIMIT:
            if not self.hit_scan_limit:
                self.hit_scan_limit = True
                self.log.warning(f"looked at {SCAN_LIMIT} jobs (SCAN_LIMIT), stopping")
                self.scraper_input.results_wanted = 0     # ends the search loop after this page
            raise Skip(STOP)
        self.scanned += 1

        title_tag = job_card.find("h3", class_="base-search-card__title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        time_tag = job_card.find("time")
        posted_ago = time_tag.get_text(strip=True) if time_tag else None
        if any(word.lower() in title.lower() for word in EXCLUDE):
            self._skip("excluded_title", f"{title!r}: excluded word in the title")
        if HOURS_OLD and (age_hours(posted_ago) or 0) > HOURS_OLD:
            self._skip("too_old", f"{title!r}: posted {posted_ago}")

        job = super()._process_job(job_card, job_id)      # fetches the job page (description, type, seniority)
        what = f"{title!r} at {job.company}"
        if self.oldest and job.date_posted and job.date_posted < self.oldest:
            self._skip("too_old", f"{what}: posted {job.date_posted}")
        if (JOB_TYPE or EXPERIENCE) and job.description is None:
            self._skip("no_details", f"{what}: job page not readable, can't check type / experience")
        if JOB_TYPE:
            types = [norm(t.value) for t in job.job_type or []]
            if not any(norm(w) in types for w in JOB_TYPE):
                self._skip("job_type", f"{what}: {', '.join(types) or 'no job type'}")
        if EXPERIENCE and not any(norm(w) in norm(job.job_level) for w in EXPERIENCE):
            self._skip("experience", f"{what}: {job.job_level or 'no seniority level'}")

        self.posted_ago[job.id] = posted_ago
        return job


def scrape():
    scraper = FilteredLinkedIn()
    jobs = scraper.scrape(ScraperInput(
        search_term=SEARCH_TERM, location=LOCATION, distance=DISTANCE, hours_old=HOURS_OLD or None,
        results_wanted=RESULTS, easy_apply=EASY_APPLY or None, linkedin_company_ids=[int(x) for x in COMPANY_IDS] or None,
        fetch_description=True, description_format=DescriptionFormat.HTML, proxies=PROXIES))
    return scraper, jobs


# ============ 2. Rows ============
JOB_TYPES = {"fulltime": "Full-time", "parttime": "Part-time", "contract": "Contract", "temporary": "Temporary",
             "internship": "Internship", "volunteer": "Volunteer"}
SAFE_TAGS = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "a", "blockquote",
             "h1", "h2", "h3", "h4", "h5", "h6"}


def clean_html(fragment):
    """LinkedIn's description HTML with only basic formatting tags and http(s) links kept (safe to embed)."""
    if not fragment:
        return None
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.name not in SAFE_TAGS:
            tag.unwrap()
            continue
        href = tag.get("href", "")
        tag.attrs = {}
        if tag.name == "a" and href.startswith(("http://", "https://")):
            tag.attrs = {"href": href, "target": "_blank", "rel": "noopener"}
    for text in soup.find_all(string=True):     # JobSpy prettifies; its line breaks inside <strong> etc. break Markdown
        if text.strip():
            text.replace_with(re.sub(r"\s+", " ", text))
        else:
            text.extract()
    return str(soup).strip()


def to_markdown(description_html):
    text = description_fields(description_html, DescriptionFormat.MARKDOWN)["description"]
    if not text:
        return None
    lines = [line if re.match(r"\s*([*+-]|\d+\.)\s", line) else line.lstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line if line.strip() else "" for line in lines)).strip()


# Description headings -> section, first match wins ("Minimum qualifications" is a qualification, not a requirement)
SECTIONS = [
    ("responsibilities", r"responsib|what you('|’)?ll do|what you will do|duties|day[- ]to[- ]day|key result"),
    ("qualifications",   r"qualification|education|eligib"),
    ("requirements",     r"requir|must[- ]have|skill|what you need|what we('|’)?re looking for|looking for|"
                         r"who you are|what you bring|you have|you('|’)?ll need|experience|expertise"),
    ("benefits",         r"benefit|perks|what we offer|we offer|why join|why you('|’)?ll (love|thrive)|compensation|rewards"),
    ("summary",          r"job description|about the (job|role|position)|role (overview|purpose|summary)|the role|"
                         r"your role|job (summary|purpose|overview)|position (summary|overview)"),
    ("about_company",    r"about (us|the company|the team)|about [A-Z]|who we are|company (description|overview)|"
                         r"our mission|our story"),
]


def section_of(heading):
    return next((key for key, pattern in SECTIONS if re.search(pattern, heading, re.I)), None)


def split_sections(markdown_text):
    """Summary, responsibilities, requirements, qualifications, benefits and about-the-company, cut out of the
    description at its bold / # headings. Best effort: postings with no headings give {}."""
    if not markdown_text:
        return {}
    # A bold heading inside a line ("... **Required Skills** Python, ...") -> its own line, when it names a section
    text = re.sub(r"[ \t]*\*\*([^*\n]{3,60}?)\*\*[ \t]*:?[ \t]*",
                  lambda m: f"\n**{m[1]}**\n" if section_of(m[1].strip(" :")) or m[1].endswith(":") else m[0],
                  markdown_text)
    sections, current = {}, None
    for line in text.splitlines():
        heading = re.fullmatch(r"\s*(?:#{1,6}\s+(.{3,80})|\*\*(.{3,80}?)\*\*\s*:?)\s*", line)
        if heading:
            name = (heading[1] or heading[2]).strip(" :*")
            current = section_of(name)
            if current:
                sections.setdefault(current, []).append(f"**{name}**")
            continue
        if current:
            sections[current].append(line)
    return {key: re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() for key, lines in sections.items()}


def experience_years(text):
    """The first "4-6 years" / "5+ years" / "2.00 + years" in the text -> {"min": 4, "max": 6}; None if there is none."""
    match = re.search(r"(?<![\d.])(\d{1,2})(?:\.\d+)?\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})(?:\.\d+)?\s*\+?\s*)?(?:years|yrs)\b",
                      text or "", re.I)
    return {"min": int(match[1]), "max": int(match[2]) if match[2] else None} if match else None


WORKPLACES = [("Hybrid", r"\bhybrid\b"), ("Remote", r"\bremote\b|work from home|\bwfh\b"),
              ("On-site", r"\bon-?site\b|\bin[- ]office\b|work from office|\bwfo\b")]
WORKPLACE_WORDS = r"hybrid|remote|on-?site|in-office|wfh|wfo|work from (?:home|office)"
# Where a description states the workplace: a labelled line ("Work mode: Hybrid", "Location: Bangalore (Onsite)"),
# a parenthesis ("(Hybrid, Bangalore)"), or a plain statement ("we embrace a hybrid working environment").
# Generic mentions ("remote, hybrid and office-based models", "hybrid cloud") don't count.
WORKPLACE_CLUES = [
    r"\b(?:work(?:ing)?\s*(?:mode|model|place|style|type|location|arrangement|setup)|opportunity type|location type|"
    r"job mode|employment mode|job location|location)\W{0,3}\s*[:\-–]\s*([^\n]{0,60})",
    rf"\(([^()\n]{{0,30}}\b(?:{WORKPLACE_WORDS})\b[^()\n]{{0,30}})\)",
    r"\b(hybrid (?:work|working)\b|\d days? (?:a week )?(?:in|from) (?:the )?office|(?:fully|100%) remote|"
    r"(?:this is a |a )remote (?:role|position|job|opportunity))",
]


def find_workplace(title, location, description):
    """Remote / Hybrid / On-site when the posting says so: in the title or location, else where the description
    states it (WORKPLACE_CLUES). LinkedIn doesn't show its own workplace field to logged-out visitors."""
    clues = [(f"{title} {location}", "title_or_location")]
    for pattern in WORKPLACE_CLUES:
        clues += [(m[1], "description") for m in re.finditer(pattern, description or "", re.I)]
    for text, source in clues:
        if re.search(r"\d days? (?:a week )?(?:in|from) (?:the )?office", text, re.I):
            return "Hybrid", source
        for workplace, pattern in WORKPLACES:
            if re.search(pattern, text, re.I):
                return workplace, source
    return None, None


def company_slug(company_url):
    match = re.search(r"/company/([^/?#]+)", company_url or "")
    return match[1].lower() if match else None


def posted_at(posted_ago, posted_date, scraped_at):
    """A UTC timestamp from "51 minutes ago" / "5 hours ago" (to the minute / hour); LinkedIn only gives the date for
    anything a day or older. Returns (posted_at, precision)."""
    hours = age_hours(posted_ago)
    if hours is not None and hours < 24:
        precise = "minute" if "minute" in (posted_ago or "") or hours == 0 else "hour"
        return (scraped_at - timedelta(hours=hours)).isoformat(timespec="minutes"), precise
    return (posted_date.isoformat() if posted_date else None), ("day" if posted_date else None)


def applicant_count(text):
    """ "102 applicants" -> 102, "Over 200 applicants" -> 200; None for "Be among the first 25 applicants"."""
    match = re.fullmatch(r"(?:over\s+)?([\d,]+)\s+applicants?", (text or "").strip(), re.I)
    return int(match[1].replace(",", "")) if match else None


# The public.jobs columns the scraper fills. status, first_seen_at (table defaults) and category, role, level, tags,
# tagged_at (set by tagging, later) are left out, so an upsert on (source, job_id) doesn't overwrite them.
ROW_FIELDS = ["source", "job_id", "title", "company", "company_url", "company_slug", "location", "workplace", "url",
              "apply_url", "posted_at", "employment_type", "seniority", "salary", "description", "search_name", "raw",
              "last_seen_at"]


def row(job, posted_ago, extras, scraped_at):
    description_html = clean_html(job.description)
    description = to_markdown(description_html)
    location = job.location.display_location() if job.location else None
    workplace, workplace_source = find_workplace(job.title, location, description)
    when, precision = posted_at(posted_ago, job.date_posted, scraped_at)
    sections = split_sections(description)
    asks = "\n".join(sections.get(key, "") for key in ("requirements", "qualifications"))
    apply_type = extras.get("apply_type")
    return {
        "source": "linkedin",
        "job_id": (job.id or "").removeprefix("li-"),
        "title": job.title,
        "company": job.company,
        "company_url": job.company_url,
        "company_slug": company_slug(job.company_url),
        "location": location,
        "workplace": workplace,
        "url": job.job_url,
        "apply_url": job.job_url if apply_type == "easy_apply" else None,   # company-site links are behind sign-in
        "posted_at": when,
        "employment_type": ", ".join(JOB_TYPES.get(t.value, t.value) for t in job.job_type or []) or None,
        "seniority": job.job_level.capitalize() if job.job_level else None,
        "salary": extras.get("salary_text"),
        "description": description,
        "search_name": SEARCH_NAME,
        "raw": {
            "page_read": bool(extras),                 # false: the job page failed, only the search card's fields
            "posted_ago": posted_ago,
            "posted_date": job.date_posted.isoformat() if job.date_posted else None,
            "posted_at_precision": precision,
            "applicants": applicant_count(extras.get("applicants_text")),
            "applicants_text": extras.get("applicants_text"),
            "apply_type": apply_type,                  # easy_apply | company_site
            "salary_min": job.min_amount,
            "salary_max": job.max_amount,
            "salary_currency": job.currency,
            "salary_interval": job.interval.value if job.interval else None,
            "job_function": job.job_function,
            "industries": job.company_industry,
            "workplace_source": workplace_source,      # title_or_location | description
            "experience_years": experience_years(asks) or experience_years(description),
            "sections": sections,
            "emails": job.emails or [],
            "company_logo": job.company_logo,
            "description_html": description_html,
            "search": dict(search_term=SEARCH_TERM, location=LOCATION, distance=DISTANCE, hours_old=HOURS_OLD),
        },
        "last_seen_at": scraped_at.isoformat(timespec="seconds"),
    }


def search_text(meta):
    s, f = meta["search"], meta["filters"]
    parts = [f"last {s['hours_old']}h" if s["hours_old"] else "any time", f"sorted by {s['sort']}"]
    parts += [f"{name.replace('_', ' ')}: {', '.join(map(str, value))}" for name, value in
              [("job_type", f["job_type"]), ("experience", f["experience"]), ("excluding", f["exclude"]),
               ("company_ids", s["company_ids"])] if value]
    if s["easy_apply"]:
        parts.append("Easy Apply only")
    return " · ".join(parts)


def skipped_text(meta):
    counts = meta["counts"]
    text = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts["skipped"].items())
    return f"skipped {text}" if text else ""


# ============ 3. Write ============
def write_json(meta, rows):
    with open(os.path.join(OUT_DIR, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "jobs": rows}, f, indent=1, ensure_ascii=False)


def write_csv(rows):
    with open(os.path.join(OUT_DIR, "jobs.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows({**r, "raw": json.dumps(r["raw"], ensure_ascii=False)} for r in rows)


DESCRIPTION_MAX = 12_000     # characters of each description in jobs.md (the rest is a link to the job)
SUMMARY_MAX = 900_000        # GitHub caps a step's run summary at 1 MiB


def markdown(meta, rows):
    """The jobs table, then each description in a collapsible block (jobs.md and the run page's summary)."""
    cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
    lines = [f"### {len(rows)} LinkedIn jobs: {meta['search']['search_term']!r} in {meta['search']['location']!r}",
             "", search_text(meta), ""]
    if rows:
        lines += ["| Job | Company | Location | Workplace | Posted | Type | Seniority | Salary | Applicants |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| [{cell(r['title'])}]({r['url']}) | {cell(r['company'])} | {cell(r['location'])} | "
                  f"{cell(r['workplace'])} | {cell(r['raw']['posted_ago'] or r['posted_at'])} | "
                  f"{cell(r['employment_type'])} | {cell(r['seniority'])} | {cell(r['salary'])} | "
                  f"{cell(r['raw']['applicants_text'])} |" for r in rows]
    counts = meta["counts"]
    lines += ["", f"Looked at {counts['scanned']} jobs, kept {counts['kept']}."]
    if skipped_text(meta):
        lines[-1] += f" {skipped_text(meta).capitalize()}."
    if counts["stopped_at_scan_limit"]:
        lines[-1] += f" Stopped at the scan limit ({SCAN_LIMIT}); loosen the filters or raise SCAN_LIMIT for more."
    if meta["error"]:
        lines += ["", f"**Error:** {cell(meta['error'])}"]
    if rows:
        lines += ["", "#### Descriptions", ""]
        size = sum(map(len, lines))
        for r in rows:
            description = r["description"] or "_No description: the job page couldn't be read._"
            if len(description) > DESCRIPTION_MAX:
                description = description[:DESCRIPTION_MAX].rsplit("\n", 1)[0] + f"\n\n… [full description]({r['url']})"
            block = [f"<details><summary><b>{html.escape(r['title'])}</b> · {html.escape(r['company'] or '')}</summary>",
                     "", description, "", "</details>", ""]
            if size + sum(map(len, block)) > SUMMARY_MAX:
                lines.append(f"_{len(rows) - rows.index(r)} more descriptions are in jobs.json / report.html._")
                break
            lines += block
            size += sum(map(len, block))
    return "\n".join(lines) + "\n"


SECTION_LABELS = {"requirements": "Requirements", "qualifications": "Qualifications"}


def bold(escaped_markdown):
    """**text** -> <b>text</b> in already-escaped Markdown (the report shows sections as text, not rendered HTML)."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped_markdown)


def report_html(meta, rows):
    e = lambda v: html.escape(str(v or ""))
    cards = []
    for r in rows:
        raw = r["raw"]
        chips = [c for c in (r["workplace"], r["employment_type"], r["seniority"], r["salary"], raw["industries"])
                 if c and c != "Not applicable"]
        text = " ".join(str(v or "") for v in (r["title"], r["company"], r["location"], r["workplace"],
                                               r["employment_type"], r["seniority"], raw["industries"])).lower()
        logo = f'<img class="logo" src="{e(raw["company_logo"])}" alt="" loading="lazy">' if raw["company_logo"] else \
               f'<div class="logo">{e((r["company"] or "?")[:1])}</div>'
        company = f'<a href="{e(r["company_url"])}" target="_blank" rel="noopener">{e(r["company"])}</a>' \
                  if r["company_url"] else e(r["company"])
        facts = [raw["applicants_text"], {"easy_apply": "Easy Apply", "company_site": "Apply on company site"}.get(raw["apply_type"])]
        if raw["experience_years"]:
            years = raw["experience_years"]
            facts.append(f"{years['min']}–{years['max']} yrs experience" if years["max"] else f"{years['min']}+ yrs experience")
        sections = "".join(f'<div class="md">{bold(e(body))}</div>'
                           for key, body in raw["sections"].items() if key in SECTION_LABELS)   # body starts with its heading
        desc = f'<details><summary>Description</summary><div class="desc">{raw["description_html"]}</div></details>' \
               if raw["description_html"] else '<p class="muted">No description (job page not readable).</p>'
        cards.append(f"""<article class="job" data-text="{e(text)}">
  {logo}
  <div class="body">
    <h2><a href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['title'])}</a></h2>
    <p class="meta">{company} · {e(r['location'])} · <span title="{e(r['posted_at'])}">{e(raw['posted_ago'] or r['posted_at'])}</span></p>
    <p class="chips">{''.join(f'<span>{e(c)}</span>' for c in chips)}</p>
    <p class="facts">{' · '.join(e(f) for f in facts if f)}</p>
    {f'<details><summary>Requirements &amp; qualifications</summary>{sections}</details>' if sections else ''}
    {desc}
  </div>
</article>""")
    counts, s = meta["counts"], meta["search"]
    error = f'<p class="error">{e(meta["error"])}</p>' if meta["error"] else ""
    if counts["stopped_at_scan_limit"]:
        error += (f'<p class="muted">Stopped at the scan limit ({SCAN_LIMIT}); loosen the filters or raise '
                  'SCAN_LIMIT for more.</p>')
    generated = datetime.fromisoformat(meta["generated_at"]).strftime("%d %b %Y, %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkedIn jobs</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2329; --muted:#5d6874; --line:#e1e5ea; --accent:#0a66c2; --chip:#eef3f8; --error:#b42318; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --card:#1a1f25; --text:#e6e9ed; --muted:#9aa5b1; --line:#2c333b; --accent:#70b5f9; --chip:#243040; --error:#f97066; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:24px 16px 48px; }}
header h1 {{ font-size:22px; margin:0 0 4px; }}
.muted, .sub {{ color:var(--muted); }} .sub {{ margin:0 0 4px; }}
.error {{ color:var(--error); }}
input {{ width:100%; margin:16px 0; padding:10px 12px; font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; }}
.job {{ display:flex; gap:14px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:12px; }}
.logo {{ flex:none; width:44px; height:44px; border-radius:8px; object-fit:contain; background:var(--chip); display:grid; place-items:center; font-weight:600; color:var(--muted); }}
.body {{ min-width:0; flex:1; }}
h2 {{ font-size:17px; margin:0; }} a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.meta {{ margin:2px 0 8px; color:var(--muted); }}
.chips {{ margin:0; display:flex; flex-wrap:wrap; gap:6px; }} .chips:empty, .facts:empty {{ display:none; }}
.chips span {{ background:var(--chip); border-radius:999px; padding:2px 10px; font-size:13px; }}
.facts {{ margin:8px 0 0; color:var(--muted); font-size:13px; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--accent); }}
.md {{ white-space:pre-wrap; overflow-wrap:anywhere; margin-top:8px; }}
.desc {{ margin-top:8px; overflow-wrap:anywhere; }} .desc p {{ margin:0 0 .6em; }}
.desc h1, .desc h2, .desc h3 {{ font-size:15px; }}
</style></head>
<body><main>
<header>
  <h1>{len(rows)} LinkedIn jobs: {e(s['search_term'])} in {e(s['location'])}</h1>
  <p class="sub">{e(search_text(meta))}</p>
  <p class="sub">Looked at {counts['scanned']}, kept {counts['kept']}{f' · {e(skipped_text(meta))}' if skipped_text(meta) else ''} · {e(generated)}</p>
  {error}
</header>
<input id="q" type="search" placeholder="Filter by title, company, location, workplace, seniority…" aria-label="Filter jobs">
<p class="muted" id="none" hidden>No jobs match.</p>
{chr(10).join(cards) or '<p class="muted">No jobs found.</p>'}
</main>
<script>
const q = document.getElementById("q"), jobs = [...document.querySelectorAll(".job")];
q.addEventListener("input", () => {{
  const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
  let shown = 0;
  for (const job of jobs) {{ job.hidden = !words.every(w => job.dataset.text.includes(w)); shown += !job.hidden; }}
  document.getElementById("none").hidden = shown > 0 || !jobs.length;
}});
</script>
</body></html>
"""


def main():
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout)
    check_settings()
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"\n=== LINKEDIN: {SEARCH_TERM!r} in {LOCATION!r}, "
          f"{f'last {HOURS_OLD}h' if HOURS_OLD else 'any time'}, {RESULTS} jobs, sorted by {SORT} ===")

    scraper, found = scrape()
    scraped_at = datetime.now(timezone.utc)
    rows = [row(job, scraper.posted_ago.get(job.id), scraper.page_extras.get(job.id, {}), scraped_at) for job in found]
    if SORT == "recent":
        rows.sort(key=lambda r: (age_hours(r["raw"]["posted_ago"]) is None, age_hours(r["raw"]["posted_ago"]) or 0))
    meta = {
        "generated_at": scraped_at.isoformat(timespec="seconds"),
        "source": "linkedin",
        "search_name": SEARCH_NAME,
        "search": dict(search_term=SEARCH_TERM, location=LOCATION, distance=DISTANCE, hours_old=HOURS_OLD,
                       sort=SORT, company_ids=COMPANY_IDS, easy_apply=EASY_APPLY, proxies=bool(PROXIES)),
        "filters": dict(job_type=JOB_TYPE, experience=EXPERIENCE, exclude=EXCLUDE),
        "counts": dict(kept=len(rows), scanned=scraper.scanned, skipped=dict(scraper.skipped),
                       without_description=sum(not r["description"] for r in rows),
                       stopped_at_scan_limit=scraper.hit_scan_limit),
        "error": scraper.error,
        "warnings": scraper.warnings,
    }

    write_json(meta, rows)
    write_csv(rows)
    md = markdown(meta, rows)
    with open(os.path.join(OUT_DIR, "jobs.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(OUT_DIR, "report.html"), "w", encoding="utf-8") as f:
        f.write(report_html(meta, rows))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)

    print(f"\n🎯 {len(rows)} jobs (looked at {scraper.scanned}, skipped {dict(scraper.skipped) or 'none'})")
    for r in rows:
        print(f"  {(r['raw']['posted_ago'] or r['posted_at'] or ''):>16}  {r['title'][:50]:<50}  "
              f"{(r['company'] or '')[:24]:<24}  {r['salary'] or ''}")
    if not rows and scraper.error:
        raise SystemExit(f"LinkedIn: {scraper.error}")


if __name__ == "__main__":
    main()
