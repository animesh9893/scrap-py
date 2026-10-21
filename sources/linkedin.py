"""LinkedIn, built on JobSpy's LinkedIn scraper (python-jobspy, pinned in requirements.txt).

LinkedIn's logged-out search only applies some filters, so the rest are checked on each job's own page:
  by LinkedIn   term, location, distance, hours_old, company_ids, easy_apply
  by us         job_type, experience (from the job page), exclude (words in the title), non-tech titles
                (search.NON_TECH), hours_old (re-checked to the hour from "5 hours ago"), sort (LinkedIn ignores
                sortBy; "recent" sorts the jobs kept, newest first)
LinkedIn ignores its remote / hybrid / on-site filter for logged-out searches and doesn't show the workplace type
on the job page, so there is no remote filter; put "remote" in the search term instead.

Jobs whose id is in `known` (already in the database, see db.known_job_ids) are counted but their job page isn't
read: they come back as Result.seen, for the database to mark as seen again.
"""

import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.util import criteria
from jobspy.model import DescriptionFormat, ScraperInput
from jobspy.util import check_status, description_fields, get_enum_from_job_type

from search import age_hours, is_non_tech, norm

NAME = "linkedin"
LABEL = "LinkedIn"

log = logging.getLogger(__name__)


# What LinkedIn's job page shows ("Employment type", "Seniority level"), normalised
JOB_TYPE_VALUES   = ["fulltime", "parttime", "contract", "temporary", "internship", "volunteer", "other"]
EXPERIENCE_VALUES = ["internship", "entrylevel", "associate", "midseniorlevel", "director", "executive"]


def problems(search):
    """What's wrong with a search, for a setting that would otherwise filter out every job (or nothing) silently."""
    problems = []
    if search.sort not in ("recent", "relevant"):
        problems.append(f"SORT must be recent or relevant, got {search.sort!r}")
    for value in search.job_type:
        if norm(value) not in JOB_TYPE_VALUES:
            problems.append(f"JOB_TYPE {value!r} isn't one of: full-time, part-time, contract, temporary, internship, "
                            "volunteer, other")
    for value in search.experience:
        if not norm(value) or not any(norm(value) in level for level in EXPERIENCE_VALUES):
            problems.append(f"EXPERIENCE {value!r} isn't one of: internship, entry level, associate, mid-senior level, "
                            "director, executive")
    for value in search.company_ids:
        if not str(value).isdigit():
            problems.append(f"COMPANY_IDS {value!r} isn't a LinkedIn company id (a number, like 1441 for Google)")
    if min(search.hours_old, search.results, search.distance) < 0 or search.results == 0:
        problems.append("HOURS_OLD and DISTANCE can't be negative, and RESULTS must be at least 1")
    return problems



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
    the filters raise Skip and the run still ends up with search.results matching jobs (or stops at scan_limit).
    """

    def __init__(self, search, known=frozenset()):
        super().__init__()
        self.search, self.known = search, known
        self.seen = []             # ids in `known` this search found again (their pages aren't read)
        self.scanned = 0
        self.hit_scan_limit = False
        self.skipped = Counter()
        self.posted_ago = {}
        self.page_extras = {}      # job id -> what _fetch_details reads beyond JobSpy's fields
        self.oldest = (datetime.now(timezone.utc) - timedelta(hours=search.hours_old, days=1)).date() \
            if search.hours_old else None
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
        s = self.search
        if self.scanned >= s.scan_limit:
            if not self.hit_scan_limit:
                self.hit_scan_limit = True
                self.log.warning(f"looked at {s.scan_limit} jobs (SCAN_LIMIT), stopping")
                self.scraper_input.results_wanted = 0     # ends the search loop after this page
            raise Skip(STOP)
        self.scanned += 1

        title_tag = job_card.find("h3", class_="base-search-card__title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        time_tag = job_card.find("time")
        posted_ago = time_tag.get_text(strip=True) if time_tag else None
        if any(word.lower() in title.lower() for word in s.exclude):
            self._skip("excluded_title", f"{title!r}: excluded word in the title")
        if is_non_tech(title):
            self._skip("non_tech", f"{title!r}: not a tech job")
        if s.hours_old and (age_hours(posted_ago) or 0) > s.hours_old:
            self._skip("too_old", f"{title!r}: posted {posted_ago}")
        if job_id in self.known:                          # in the database already: no need to read its page again
            self.seen.append(job_id)
            self._skip("already_saved", f"{title!r}: already saved")

        job = super()._process_job(job_card, job_id)      # fetches the job page (description, type, seniority)
        what = f"{title!r} at {job.company}"
        if self.oldest and job.date_posted and job.date_posted < self.oldest:
            self._skip("too_old", f"{what}: posted {job.date_posted}")
        if (s.job_type or s.experience) and job.description is None:
            self._skip("no_details", f"{what}: job page not readable, can't check type / experience")
        if s.job_type:
            types = [norm(t.value) for t in job.job_type or []]
            if not any(norm(w) in types for w in s.job_type):
                self._skip("job_type", f"{what}: {', '.join(types) or 'no job type'}")
        if s.experience and not any(norm(w) in norm(job.job_level) for w in s.experience):
            self._skip("experience", f"{what}: {job.job_level or 'no seniority level'}")

        self.posted_ago[job.id] = posted_ago
        return job


def scrape(search, known=frozenset()):
    scraper = FilteredLinkedIn(search, known)
    jobs = scraper.scrape(ScraperInput(
        search_term=search.term, location=search.where, distance=search.distance, hours_old=search.hours_old or None,
        results_wanted=search.results, easy_apply=search.easy_apply or None,
        linkedin_company_ids=[int(x) for x in search.company_ids] or None,
        fetch_description=True, description_format=DescriptionFormat.HTML, proxies=list(search.proxies)))
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


def row(job, posted_ago, extras, scraped_at, search):
    description_html = clean_html(job.description)
    description = to_markdown(description_html)
    location = job.location.display_location() if job.location else None
    workplace, workplace_source = find_workplace(job.title, location, description)
    when, precision = posted_at(posted_ago, job.date_posted, scraped_at)
    sections = split_sections(description)
    asks = "\n".join(sections.get(key, "") for key in ("requirements", "qualifications"))
    apply_type = extras.get("apply_type")
    return {
        "source": NAME,
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
        "search_name": search.name,
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
            "search": dict(search_term=search.term, location=search.location, distance=search.distance,
                           hours_old=search.hours_old),
        },
        "last_seen_at": scraped_at.isoformat(timespec="seconds"),
    }


@dataclass
class Result:
    rows: list      # the jobs kept, as public.jobs rows
    meta: dict      # the search, filters, counts, error and warnings (jobs.json's "meta")
    seen: list      # ids of known jobs found again, their pages not read


def run(search, known=frozenset()):
    """Scrapes one search. A failed search comes back with meta["error"] set and the jobs found before it failed."""
    scraper, found = scrape(search, known)
    scraped_at = datetime.now(timezone.utc)
    rows = [row(job, scraper.posted_ago.get(job.id), scraper.page_extras.get(job.id, {}), scraped_at, search)
            for job in found]
    if search.sort == "recent":
        rows.sort(key=lambda r: (age_hours(r["raw"]["posted_ago"]) is None, age_hours(r["raw"]["posted_ago"]) or 0))
    meta = {
        "generated_at": scraped_at.isoformat(timespec="seconds"),
        "source": NAME,
        "source_label": LABEL,
        "search_name": search.name,
        "search": search.describe(),
        "filters": search.filters(),
        "counts": dict(kept=len(rows), scanned=scraper.scanned, skipped=dict(scraper.skipped),
                       without_description=sum(not r["description"] for r in rows),
                       stopped_at_scan_limit=scraper.hit_scan_limit, scan_limit=search.scan_limit),
        "error": scraper.error,
        "warnings": scraper.warnings,
    }
    return Result(rows, meta, scraper.seen)
