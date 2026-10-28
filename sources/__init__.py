"""Job platforms. Each is a module with the same small interface, so the planner, scrape.py and db.py never depend
on one platform:

  NAME                     its sources.id and jobs.source ('linkedin')
  LABEL                    how it's shown ('LinkedIn')
  problems(search)         what's wrong with a search's settings for this platform (a list of messages)
  run(search, known)       scrapes one search -> Result(rows, meta, seen): rows shaped like public.jobs, meta
                           (counts, error), and the ids in `known` (jobs already saved) it found again

Company job boards (Ashby, sources/ashby.py) aren't searched by keyword and location, so they aren't in AVAILABLE:
boards.py reads them.

To add a platform (Naukri, …): add sources/<name>.py with that interface, register it below, and add a row to the
sources table. Locations name the platform's own spelling of a place in locations.source_params.<name>.location.
"""

from importlib import import_module

# The public.jobs columns every source fills. status, first_seen_at (table defaults) and category, role, level, tags,
# tagged_at (set by tagging, later) are left out, so an upsert on (source, job_id) doesn't overwrite them.
ROW_FIELDS = ["source", "job_id", "title", "company", "company_url", "company_slug", "location", "workplace", "url",
              "apply_url", "posted_at", "employment_type", "seniority", "salary", "description", "search_name", "raw",
              "last_seen_at"]

AVAILABLE = ["linkedin"]


def get(name):
    if name not in AVAILABLE:
        raise KeyError(f"unknown source {name!r}; available: {', '.join(AVAILABLE)}")
    return import_module(f"sources.{name}")
