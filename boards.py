"""Reads companies' own job boards (Ashby, sources/ashby.py) and saves their tech jobs to Supabase with source 'ashby',
for the site's Ashby section. Runs on GitHub Actions (.github/workflows/boards.yml).

The companies come from the companies table (companies.ats, ats_slug), which db.py fills from data/ats-companies.csv.
Each run reads the boards not read in the last BOARD_INTERVAL_HOURS, never-read ones first, WORKERS at a time, until
TIME_BUDGET_MINUTES; the rest wait for the next run. Each board is one request to Ashby's public posting API that
returns all its jobs, so a job the board no longer lists is closed (and leaves the site) as soon as its board is
read again.

Settings (environment variables):
  DATABASE_URL           Supabase Postgres; unset = print what a board has (needs BOARD), save nothing
  BOARD                  only this board (jobs.ashbyhq.com/<BOARD>), due or not
  MAX_BOARDS             at most this many boards a run (0 = every due board)
  BOARD_INTERVAL_HOURS   how often a board is read (6)
  MAX_AGE_DAYS           only jobs published in the last N days (30; 0 = any)
  WORKERS                boards read at once (6)
  TIME_BUDGET_MINUTES    no new board starts after this (40)
  DRY_RUN                true: read, don't save
"""

import logging
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from search import env, env_bool, env_int
from sources import ashby

DATABASE_URL        = env("DATABASE_URL", None)
DRY_RUN             = env_bool("DRY_RUN", False)
BOARD               = env("BOARD", "").strip()
MAX_BOARDS          = env_int("MAX_BOARDS", 0)
INTERVAL_HOURS      = env_int("BOARD_INTERVAL_HOURS", 6)
MAX_AGE_DAYS        = env_int("MAX_AGE_DAYS", 30)
WORKERS             = env_int("WORKERS", 6)
TIME_BUDGET_MINUTES = env_int("TIME_BUDGET_MINUTES", 40)
EXPIRE_DAYS         = env_int("EXPIRE_DAYS", 7)

log = logging.getLogger(__name__)
local = threading.local()


def session():
    if not hasattr(local, "session"):
        local.session = requests.Session()
        local.session.headers["User-Agent"] = "krawlr.xyz job board reader (+https://krawlr.xyz)"
    return local.session


def read(board, started):
    """(data, status, error) for one board, retrying a failed request once."""
    if time.monotonic() - started > TIME_BUDGET_MINUTES * 60:
        return None, "skipped", None
    for attempt in range(2):
        try:
            return ashby.fetch(session(), board), "ok", None
        except ashby.NotFound:
            return None, "not_found", "Ashby has no job board by this name"
        except (requests.RequestException, ValueError) as e:
            if attempt:
                return None, "error", str(e)[:500]
            time.sleep(5)


def step_summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout)
    saving = bool(DATABASE_URL) and not DRY_RUN
    conn = None
    if DATABASE_URL:
        import db
        conn = db.connect(DATABASE_URL)     # also imports data/ats-companies.csv when it changed
        boards = db.due_boards(conn, ashby.NAME, INTERVAL_HOURS, MAX_BOARDS or None, BOARD or None)
    elif BOARD:
        boards = [(None, BOARD, BOARD)]
    else:
        raise SystemExit("Set DATABASE_URL to read the boards in the companies table, or BOARD to read one board")
    if BOARD and not boards:
        raise SystemExit(f"No company has the Ashby board {BOARD!r}: add it to data/ats-companies.csv")
    print(f"📋 {len(boards)} Ashby boards to read, {WORKERS} at a time"
          f"{'' if saving else ' (not saving)'}")

    started, totals, statuses, skipped = time.monotonic(), Counter(), Counter(), Counter()
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(read, board, started): (slug, name, board) for slug, name, board in boards}
        for future in as_completed(futures):
            slug, name, board = futures[future]
            data, status, error = future.result()
            statuses[status] += 1
            if status == "skipped":
                continue
            rows, counts = ashby.rows(data, name, board, max_age_days=MAX_AGE_DAYS) if data else ([], {})
            skipped.update(counts.get("skipped", {}))
            totals.update(listed=counts.get("listed", 0), kept=counts.get("kept", 0))
            line = f"  {board:<32} {status:<9}"
            if status == "ok":
                line += f" {counts['listed']:>5} listed, {counts['kept']:>4} tech jobs kept"
            if saving:
                conn = db.alive(conn, DATABASE_URL)
                saved = db.save_board(conn, ashby.NAME, slug, rows, counts, status, error)
                totals.update(saved)
                if status == "ok":
                    line += f" · {saved['new']} new, {saved['closed']} closed"
            print(line + (f" · {error}" if error else ""))
            if BOARD and not saving:
                for r in rows:
                    print(f"    {r['posted_at'][:10] if r['posted_at'] else '':<10}  {r['title'][:60]:<60}  "
                          f"{(r['location'] or '')[:30]:<30}  {r['salary'] or ''}")

    summary = (f"Ashby: read {statuses['ok']} boards ({statuses['not_found']} not found, {statuses['error']} failed, "
               f"{statuses['skipped']} left for the next run): {totals['listed']} jobs listed, {totals['kept']} tech "
               f"jobs from the last {MAX_AGE_DAYS or 'any number of'} days kept"
               + (f", {totals['new']} new, {totals['closed']} closed" if saving else "")
               + f". Left out: {dict(skipped) or 'none'}.")
    if saving:
        conn = db.alive(conn, DATABASE_URL)
        db.record_run(conn, ashby.NAME, BOARD or "ashby boards", {**totals, **{f"boards_{k}": v for k, v in statuses.items()},
                                                                   "skipped": dict(skipped)},
                      None if statuses["ok"] or not boards else "every board failed")
        done = db.finish(conn, EXPIRE_DAYS)
        summary += f" Closed {done['closed']} jobs not seen for {EXPIRE_DAYS} days."
    print(f"\n💾 {summary}")
    step_summary(summary + "\n")
    if conn:
        conn.close()
    if boards and not statuses["ok"] and statuses["error"]:
        raise SystemExit("Every board failed: is api.ashbyhq.com reachable?")


if __name__ == "__main__":
    main()
