"""Krawlr job scraper: runs searches on job platforms (sources/, LinkedIn for now), writes what it finds to OUT_DIR
(report.py), and saves it to Supabase when DATABASE_URL is set (db.py). Runs on GitHub Actions
(.github/workflows/scrape.yml), which uploads OUT_DIR as the run's artifact.

Two modes (MODE):
  single   one search, described by the environment (the workflow's inputs): SEARCH_TERM, LOCATION and the
           filters in search.py. Writes jobs.json, jobs.csv, jobs.md and report.html to OUT_DIR.
  plan     the searches that are due from the search_keywords × locations lists in the database (planner.py), at
           most MAX_TASKS of them, stopping early after TIME_BUDGET_MINUTES. Each search's files go to its own folder
           in OUT_DIR, with summary.md listing them. The environment's filters are the defaults for every search.
  sweep    every pair from the lists, due or not, each looking back the full HOURS_OLD, one after another with
           GAP_SECONDS between them (.github/workflows/sweep.yml). No MAX_TASKS; TIME_BUDGET_MINUTES defaults to 330.
  plan-only  prints what `plan` would search, and does nothing else (with PLAN_ALL=true, what `sweep` would).

Jobs already in the database (seen in the last KNOWN_DAYS) are only marked as seen again, without reading their job
pages, when SKIP_KNOWN is on (default: on in plan mode, off for a single search, so its report shows every job).
Every setting can be overridden with an environment variable of the same name. Lists are comma-separated.
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import report
import sources
from search import env, env_bool, env_int, from_env

MODE                 = env("MODE", "single").lower()   # single | plan | sweep | plan-only
SWEEP                = MODE == "sweep" or env_bool("PLAN_ALL", False)
OUT_DIR              = env("OUT_DIR", "out")
DATABASE_URL         = env("DATABASE_URL", None)       # Supabase Postgres; unset = files only
DRY_RUN              = env_bool("DRY_RUN", False)      # true: files only, even with DATABASE_URL set
EXPIRE_DAYS          = env_int("EXPIRE_DAYS", 7)       # jobs not seen for this many days are marked closed
KNOWN_DAYS           = env_int("KNOWN_DAYS", EXPIRE_DAYS)
SKIP_KNOWN           = env_bool("SKIP_KNOWN", MODE != "single")
MAX_TASKS            = env_int("MAX_TASKS", 100000 if SWEEP else 4)   # searches per planned run
DEFAULT_INTERVAL     = env_int("DEFAULT_INTERVAL_HOURS", 2)   # how often a pair is due, unless the lists say
TIME_BUDGET_MINUTES  = env_int("TIME_BUDGET_MINUTES", 330 if SWEEP else 8)   # no new search starts after this (the
                                                                             # schedule is 10 min, a job at most 6 h)
GAP_SECONDS          = env_int("GAP_SECONDS", 60 if SWEEP else 0)            # pause between searches

log = logging.getLogger(__name__)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def step_summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)


def print_jobs(result):
    meta = result.meta
    print(f"\n🎯 {len(result.rows)} jobs (looked at {meta['counts']['scanned']}, "
          f"skipped {meta['counts']['skipped'] or 'none'})")
    for r in result.rows:
        print(f"  {(r['raw']['posted_ago'] or r['posted_at'] or ''):>16}  {r['title'][:50]:<50}  "
              f"{(r['company'] or '')[:24]:<24}  {r['salary'] or ''}")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout)
    if MODE not in ("single", "plan", "sweep", "plan-only"):
        raise SystemExit(f"MODE must be single, plan, sweep or plan-only, got {MODE!r}")
    defaults = from_env()
    saving = bool(DATABASE_URL) and not DRY_RUN and MODE != "plan-only"
    if MODE != "single" and not DATABASE_URL:
        raise SystemExit(f"MODE={MODE} reads the search lists from the database: set DATABASE_URL")
    if not DATABASE_URL:
        print("DATABASE_URL isn't set: files only, nothing saved to Supabase")
    elif DRY_RUN:
        print("DRY_RUN: files only, nothing saved to Supabase")

    conn = None
    if DATABASE_URL:
        import db     # psycopg is only needed with a database
        conn = db.connect(DATABASE_URL)

    plan_note = ""
    if MODE == "single":
        problems = sources.get(defaults.source).problems(defaults)
        if problems:
            raise SystemExit("Invalid settings:\n  " + "\n  ".join(problems))
        searches = [db.ensure_search(conn, defaults) if saving else defaults]
    else:
        import planner
        planned, due, lists = planner.plan(conn.cursor(), sources.AVAILABLE, defaults, datetime.now(timezone.utc),
                                           default_interval_hours=DEFAULT_INTERVAL, max_tasks=MAX_TASKS,
                                           due_only=not SWEEP)
        searches = [p.search for p in planned]
        plan_note = (f"{lists['keywords']} active keywords × {lists['locations']} active locations × "
                     f"{', '.join(lists['sources']) or 'no enabled sources'}: {due} {'pairs' if SWEEP else 'due'}, "
                     f"running {len(searches)}"
                     + (f", {GAP_SECONDS}s apart." if SWEEP else f" (MAX_TASKS {MAX_TASKS})."))
        print(f"\n📋 {plan_note}")
        for p in planned:
            print(f"  {p.search.source:<9} {p.search.name:<50} last {p.search.hours_old}h"
                  f"{'' if p.last_ok else '  (never run)'}")
        if not lists["keywords"] or not lists["locations"]:
            print("Nothing to search: add rows to search_keywords and locations (see seed.sql)")
        if MODE == "plan-only":
            return

    os.makedirs(OUT_DIR, exist_ok=True)
    started, results = time.monotonic(), []
    known = {}
    for i, search in enumerate(searches, 1):
        if i > 1 and GAP_SECONDS:
            time.sleep(GAP_SECONDS)
        if MODE != "single" and time.monotonic() - started > TIME_BUDGET_MINUTES * 60:
            print(f"\n⏱  {TIME_BUDGET_MINUTES} minutes used (TIME_BUDGET_MINUTES): leaving "
                  f"{len(searches) - i + 1} searches for the next run")
            break
        source = sources.get(search.source)
        print(f"\n=== {source.LABEL.upper()}: {search.term!r} in {search.location!r}, "
              f"{f'last {search.hours_old}h' if search.hours_old else 'any time'}, {search.results} jobs, "
              f"sorted by {search.sort} ===")
        problems = source.problems(search)
        if problems:
            print("Skipping, invalid settings:\n  " + "\n  ".join(problems))
            continue
        if SKIP_KNOWN and conn and search.source not in known:
            known[search.source] = set(db.known_job_ids(conn, search.source, KNOWN_DAYS))
        result = source.run(search, frozenset(known.get(search.source, ())))

        folder = "." if MODE == "single" else f"{i:02d}-{search.source}-{slug(search.name)}"
        md = report.write(os.path.join(OUT_DIR, folder), result.meta, result.rows)
        if MODE == "single":
            step_summary(md)
        print_jobs(result)

        saved = None
        if saving:
            conn = db.alive(conn, DATABASE_URL)     # a sweep runs for hours; the pooler may drop an idle connection
            saved = db.save(conn, search, result)
            if search.source in known:      # this run's later searches only mark them seen
                known[search.source].update(r["job_id"] for r in result.rows)
            print(f"💾 {saved['new']} new, {saved['updated']} seen before, {saved['seen']} already saved and found again "
                  f"(run {saved['run_id']})")
        results.append(dict(meta=result.meta, saved=saved, folder=folder))

    if saving:
        conn = db.alive(conn, DATABASE_URL)
        done = db.finish(conn, EXPIRE_DAYS)
        line = (f"Saved to Supabase: {sum(r['saved']['new'] for r in results)} new jobs; linked {done['linked']} to "
                f"companies, closed {done['closed']} not seen for {EXPIRE_DAYS} days.")
        print(f"\n💾 {line}")
        step_summary(f"\n{line}\n")
    if MODE != "single":
        text = report.summary(results, plan_note)
        with open(os.path.join(OUT_DIR, "summary.md"), "w", encoding="utf-8") as f:
            f.write(text)
        step_summary(text)
    if conn:
        conn.close()

    failed = [r for r in results if r["meta"]["error"] and not r["meta"]["counts"]["kept"]]
    if results and len(failed) == len(results):
        raise SystemExit(f"{failed[0]['meta']['source_label']}: {failed[0]['meta']['error']}")


if __name__ == "__main__":
    main()
