"""Krawlr LinkedIn job scraper, built on JobSpy's LinkedIn scraper (python-jobspy, pinned in requirements.txt).

Runs on GitHub Actions (.github/workflows/scrape.yml) and writes to OUT_DIR (default out/), which the workflow
uploads as the run's artifact:
  jobs.json    {"meta": search, filters, counts, errors; "jobs": [...]}: the data, for scripts / n8n
  jobs.csv     the same jobs, one row each (description as Markdown), for Excel / Sheets
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

from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.model import DescriptionFormat, ScraperInput
from jobspy.util import description_fields


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


# ============ 1. Scrape ============
class FilteredLinkedIn(LinkedIn):
    """JobSpy's LinkedIn scraper, plus the filters LinkedIn ignores for logged-out searches.

    JobSpy's search loop skips a job when _process_job raises and keeps paging until it has results_wanted jobs, so
    the filters raise Skip and the run still ends up with RESULTS matching jobs (or stops at SCAN_LIMIT).
    """

    def __init__(self):
        super().__init__()
        self.scanned = 0
        self.hit_scan_limit = False
        self.skipped = Counter()
        self.posted_ago = {}
        self.oldest = (datetime.now(timezone.utc) - timedelta(hours=HOURS_OLD, days=1)).date() if HOURS_OLD else None
        self.log.addFilter(lambda rec: rec.getMessage() != f"skipping job: {STOP}")   # said once, below

    def _fetch_details(self, job_id):
        time.sleep(random.uniform(*DETAIL_DELAY))
        return super()._fetch_details(job_id)

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


# ============ 2. Clean records ============
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


def record(job, posted_ago):
    description_html = clean_html(job.description)
    return {
        "id": (job.id or "").removeprefix("li-"),
        "title": job.title,
        "company": job.company,
        "location": job.location.display_location() if job.location else None,
        "posted_date": job.date_posted.isoformat() if job.date_posted else None,
        "posted_ago": posted_ago,
        "employment_type": ", ".join(JOB_TYPES.get(t.value, t.value) for t in job.job_type or []) or None,
        "seniority": job.job_level.capitalize() if job.job_level else None,
        "job_function": job.job_function,
        "industries": job.company_industry,
        "salary_min": job.min_amount,
        "salary_max": job.max_amount,
        "salary_currency": job.currency,
        "salary_interval": job.interval.value if job.interval else None,
        "salary_source": (job.salary_source or "linkedin") if job.min_amount else None,
        "emails": job.emails or [],
        "url": job.job_url,
        "company_url": job.company_url,
        "company_logo": job.company_logo,
        "description": to_markdown(description_html),
        "description_html": description_html,
    }


def salary_text(job):
    if not job["salary_min"]:
        return None
    low, high = f"{job['salary_min']:,.0f}", f"{job['salary_max']:,.0f}" if job["salary_max"] else None
    amount = f"{low}–{high}" if high and high != low else low
    per = {"yearly": "yr", "monthly": "mo", "weekly": "wk", "daily": "day", "hourly": "hr"}.get(job["salary_interval"])
    return f"{job['salary_currency'] or ''} {amount}{f' / {per}' if per else ''}".strip()


def search_text(meta):
    s, f = meta["search"], meta["filters"]
    parts = [f"last {s['hours_old']}h" if s["hours_old"] else "any time", f"sorted by {s['sort']}"]
    parts += [f"{name.replace('_', ' ')}: {', '.join(map(str, value))}" for name, value in
              [("job_type", f["job_type"]), ("experience", f["experience"]), ("excluding", f["exclude"]),
               ("company_ids", s["company_ids"])] if value]
    if s["easy_apply"]:
        parts.append("Easy Apply only")
    return " · ".join(parts)


# ============ 3. Write ============
def write_json(meta, jobs):
    with open(os.path.join(OUT_DIR, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "jobs": jobs}, f, indent=1, ensure_ascii=False)


CSV_FIELDS = ["id", "title", "company", "location", "posted_date", "posted_ago", "employment_type", "seniority",
              "job_function", "industries", "salary_min", "salary_max", "salary_currency", "salary_interval",
              "salary_source", "emails", "url", "company_url", "company_logo", "description"]   # no description_html


def write_csv(jobs):
    with open(os.path.join(OUT_DIR, "jobs.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**j, "emails": ", ".join(j["emails"])} for j in jobs)


def markdown(meta, jobs):
    cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
    lines = [f"### {len(jobs)} LinkedIn jobs: {meta['search']['search_term']!r} in {meta['search']['location']!r}",
             "", search_text(meta), ""]
    if jobs:
        lines += ["| Job | Company | Location | Posted | Type | Seniority | Salary |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| [{cell(j['title'])}]({j['url']}) | {cell(j['company'])} | {cell(j['location'])} | "
                  f"{cell(j['posted_ago'] or j['posted_date'])} | {cell(j['employment_type'])} | "
                  f"{cell(j['seniority'])} | {cell(salary_text(j))} |" for j in jobs]
    counts = meta["counts"]
    lines += ["", f"Looked at {counts['scanned']} jobs, kept {counts['kept']}."]
    if counts["skipped"]:
        lines[-1] += " Skipped: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts["skipped"].items()) + "."
    if counts["stopped_at_scan_limit"]:
        lines[-1] += f" Stopped at the scan limit ({SCAN_LIMIT}); loosen the filters or raise SCAN_LIMIT for more."
    if meta["error"]:
        lines += ["", f"**Error:** {cell(meta['error'])}"]
    return "\n".join(lines) + "\n"


def report_html(meta, jobs):
    e = lambda v: html.escape(str(v or ""))
    cards = []
    for j in jobs:
        chips = [c for c in (j["employment_type"], j["seniority"], salary_text(j), j["industries"])
                 if c and c != "Not applicable"]
        text = " ".join(str(j[k] or "") for k in ("title", "company", "location", "employment_type", "seniority",
                                                    "job_function", "industries")).lower()
        logo = f'<img class="logo" src="{e(j["company_logo"])}" alt="" loading="lazy">' if j["company_logo"] else \
               f'<div class="logo">{e((j["company"] or "?")[:1])}</div>'
        company = f'<a href="{e(j["company_url"])}" target="_blank" rel="noopener">{e(j["company"])}</a>' \
                  if j["company_url"] else e(j["company"])
        desc = f'<details><summary>Description</summary><div class="desc">{j["description_html"]}</div></details>' \
               if j["description_html"] else '<p class="muted">No description (job page not readable).</p>'
        cards.append(f"""<article class="job" data-text="{e(text)}">
  {logo}
  <div class="body">
    <h2><a href="{e(j['url'])}" target="_blank" rel="noopener">{e(j['title'])}</a></h2>
    <p class="meta">{company} · {e(j['location'])} · <span title="{e(j['posted_date'])}">{e(j['posted_ago'] or j['posted_date'])}</span></p>
    <p class="chips">{''.join(f'<span>{e(c)}</span>' for c in chips)}</p>
    {desc}
  </div>
</article>""")
    counts, s = meta["counts"], meta["search"]
    skipped = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts["skipped"].items())
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
.chips {{ margin:0; display:flex; flex-wrap:wrap; gap:6px; }} .chips:empty {{ display:none; }}
.chips span {{ background:var(--chip); border-radius:999px; padding:2px 10px; font-size:13px; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--accent); }}
.desc {{ margin-top:8px; overflow-wrap:anywhere; }} .desc p {{ margin:0 0 .6em; }}
.desc h1, .desc h2, .desc h3 {{ font-size:15px; }}
</style></head>
<body><main>
<header>
  <h1>{len(jobs)} LinkedIn jobs: {e(s['search_term'])} in {e(s['location'])}</h1>
  <p class="sub">{e(search_text(meta))}</p>
  <p class="sub">Looked at {counts['scanned']}, kept {counts['kept']}{f' · skipped {e(skipped)}' if skipped else ''} · {e(generated)}</p>
  {error}
</header>
<input id="q" type="search" placeholder="Filter by title, company, location, seniority…" aria-label="Filter jobs">
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
    jobs = [record(job, scraper.posted_ago.get(job.id)) for job in found]
    if SORT == "recent":
        jobs.sort(key=lambda j: (age_hours(j["posted_ago"]) is None, age_hours(j["posted_ago"]) or 0))
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "linkedin",
        "search": dict(search_term=SEARCH_TERM, location=LOCATION, distance=DISTANCE, hours_old=HOURS_OLD,
                       sort=SORT, company_ids=COMPANY_IDS, easy_apply=EASY_APPLY, proxies=bool(PROXIES)),
        "filters": dict(job_type=JOB_TYPE, experience=EXPERIENCE, exclude=EXCLUDE),
        "counts": dict(kept=len(jobs), scanned=scraper.scanned, skipped=dict(scraper.skipped),
                       without_description=sum(not j["description"] for j in jobs),
                       stopped_at_scan_limit=scraper.hit_scan_limit),
        "error": scraper.error,
        "warnings": scraper.warnings,
    }

    write_json(meta, jobs)
    write_csv(jobs)
    md = markdown(meta, jobs)
    with open(os.path.join(OUT_DIR, "jobs.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(OUT_DIR, "report.html"), "w", encoding="utf-8") as f:
        f.write(report_html(meta, jobs))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)

    print(f"\n🎯 {len(jobs)} jobs (looked at {scraper.scanned}, skipped {dict(scraper.skipped) or 'none'})")
    for j in jobs:
        print(f"  {(j['posted_ago'] or j['posted_date'] or ''):>16}  {j['title'][:55]:<55}  {(j['company'] or '')[:30]}")
    if not jobs and scraper.error:
        raise SystemExit(f"LinkedIn: {scraper.error}")


if __name__ == "__main__":
    main()
