"""One search's settings (Search), from the environment or from a planned keyword × location (planner.py), and the
environment helpers every module reads its settings with. Lists are comma-separated.
"""

import os
import re
from dataclasses import dataclass, field, replace




def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """KEY=value lines from .env (next to the scripts) into the environment, for local runs. Variables already set
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




load_dotenv()


@dataclass(frozen=True)
class Search:
    """What one source is asked for: the search, LinkedIn's filters and ours (see scrape.py's docstring)."""
    term: str
    location: str
    name: str                       # stored in each job's search_name
    source: str = "linkedin"        # sources.id, also jobs.source
    distance: int = 50              # miles around location
    hours_old: int = 24             # only jobs posted in the last N hours; 0 = any time
    results: int = 30               # jobs to keep, after filtering
    sort: str = "recent"            # recent (newest first) | relevant (the source's order)
    company_ids: tuple = ()         # LinkedIn company ids (f_C), numbers
    easy_apply: bool = False
    job_type: tuple = ()            # full-time, part-time, contract, temporary, internship, volunteer
    experience: tuple = ()          # internship, entry level, associate, mid-senior level, director, executive
    exclude: tuple = ()             # drop jobs with any of these words in the title
    scan_limit: int = 150           # stop after looking at this many jobs (each is one request)
    proxies: tuple = field(default=(), repr=False)   # user:pass@host:port
    category: str | None = None     # categories.slug for the jobs found; None = from the keyword or title
    keyword_id: int | None = None   # search_keywords.id / locations.id, when it came from the lists
    location_id: int | None = None
    source_location: str | None = None   # the source's own name for the location (locations.source_params)

    @property
    def where(self):
        """The location as the source is asked for it."""
        return self.source_location or self.location

    def but(self, **changes):
        return replace(self, **changes)

    def describe(self):
        return dict(search_term=self.term, location=self.location, distance=self.distance, hours_old=self.hours_old,
                    sort=self.sort, company_ids=list(self.company_ids), easy_apply=self.easy_apply,
                    proxies=bool(self.proxies))

    def filters(self):
        return dict(job_type=list(self.job_type), experience=list(self.experience), exclude=list(self.exclude))


def from_env():
    """The search the environment (the workflow's inputs, or .env) describes; also the defaults for planned ones."""
    term, location = env("SEARCH_TERM", "software engineer"), env("LOCATION", "Bengaluru")
    results = env_int("RESULTS", 30)
    return Search(
        term=term, location=location, name=env("SEARCH_NAME", f"{term} in {location}"),
        distance=env_int("DISTANCE", 50), hours_old=env_int("HOURS_OLD", 24), results=results,
        sort=env("SORT", "recent").lower(), company_ids=tuple(env_list("COMPANY_IDS", [])),
        easy_apply=env_bool("EASY_APPLY", False), job_type=tuple(env_list("JOB_TYPE", [])),
        experience=tuple(env_list("EXPERIENCE", [])), exclude=tuple(env_list("EXCLUDE", [])),
        scan_limit=env_int("SCAN_LIMIT", results * 5), proxies=tuple(env_list("PROXIES", [])),
        category=env("CATEGORY", None))


def norm(text):
    """Letters only, lower case: "Mid-Senior level" -> "midseniorlevel", "full-time" -> "fulltime"."""
    return re.sub(r"[^a-z]", "", (text or "").lower())


# Titles that aren't tech jobs, skipped whatever the search: a "data analyst" or "cloud" search also finds sales,
# recruiting and support roles. Whole words, any case.
NON_TECH = re.compile(r"\b(" + "|".join([
    r"sales", r"marketing(?! cloud)", r"recruit(?:er|ers|ment|ing)", r"talent acquisition", r"human resources", r"hr",
    r"business development", r"account (?:executive|manager)", r"accountant", r"accounts", r"finance manager",
    r"customer (?:support|service|success|care)", r"tele ?(?:caller|sales)", r"call cent(?:er|re)", r"bpo",
    r"content writer", r"copywriter", r"graphic designer", r"teacher", r"tutor", r"faculty", r"professor",
    r"nurse", r"doctor", r"pharmacist", r"receptionist", r"(?:delivery|cab|truck|car) driver",
    r"delivery (?:boy|executive|partner)", r"warehouse", r"store manager", r"field executive", r"insurance (?:agent|advisor)",
    r"(?:mechanical|civil|chemical|electrical) engineer",
]) + r")\b", re.I)


def is_non_tech(title):
    return bool(NON_TECH.search(title or ""))


HOURS_PER = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 24 * 7, "month": 24 * 30, "year": 24 * 365}


def age_hours(posted_ago):
    """ "5 hours ago" -> 5, "2 weeks ago" -> 336, "Just now" -> 0; None when unknown."""
    if "just now" in (posted_ago or "").lower():
        return 0
    match = re.match(r"(\d+)\s+(minute|hour|day|week|month|year)", posted_ago or "")
    return int(match[1]) * HOURS_PER[match[2]] if match else None
