"""db.py and planner.plan() against a real Postgres that has the old job-scraper's schema (its src/db.js SCHEMA).
Skipped unless TEST_DATABASE_URL is set; the test empties the tables it uses, so never point it at Supabase."""

import os
from datetime import datetime, timezone

import pytest

from search import Search
from test_linkedin import run as scrape

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL isn't set")
DEFAULTS = Search(term="", location="", name="", results=10, scan_limit=50)


@pytest.fixture
def conn(monkeypatch):
    import db
    monkeypatch.setenv("DATABASE_SSL", "false")
    conn = db.connect(URL)
    conn.execute("TRUNCATE jobs, runs, companies, job_search_hits, search_keywords, locations CASCADE")
    with open(os.path.join(os.path.dirname(db.SCHEMA), "seed.sql"), encoding="utf-8") as f:
        conn.execute(f.read())
    yield conn
    conn.close()


def plan(conn, max_tasks):
    import planner
    return planner.plan(conn.cursor(), ["linkedin"], DEFAULTS, datetime.now(timezone.utc),
                        default_interval_hours=2, max_tasks=max_tasks)


def test_plan_save_and_skip_known(conn, monkeypatch):
    import db
    planned, due, lists = plan(conn, max_tasks=2)
    assert (lists["keywords"], lists["locations"], due, len(planned)) == (9, 7, 63, 2)

    first = planned[0].search
    result, _ = scrape(monkeypatch, first)
    saved = db.save(conn, first, result)
    assert (saved["new"], saved["updated"], saved["seen"]) == (3, 0, 0)
    done = db.finish(conn, 7)
    assert done["linked"] == 3

    jobs = conn.execute("SELECT job_id, category, workplace, company_slug FROM jobs ORDER BY job_id").fetchall()
    assert [j[1] for j in jobs] == [first.category] * 3                # the keyword's category
    assert {j[2] for j in jobs} == {"hybrid"} and all(j[3] for j in jobs)
    hits = conn.execute("SELECT keyword_id, location_id FROM job_search_hits").fetchall()
    assert set(hits) == {(first.keyword_id, first.location_id)}

    # The pair just run isn't due any more.
    planned, due, _ = plan(conn, max_tasks=100)
    assert due == 62 and first.name not in [p.search.name for p in planned]

    # Found again by another search: their pages aren't read, they're marked seen and get a hit for that search.
    second = planned[0].search
    known = db.known_job_ids(conn, "linkedin", 7)
    result, session = scrape(monkeypatch, second, known)
    assert result.rows == [] and len(result.seen) == 3
    assert not [u for u in session.urls if "/jobs/view/" in u]
    saved = db.save(conn, second, result)
    assert (saved["new"], saved["seen"]) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM job_search_hits").fetchone()[0] == 6
    runs = conn.execute("SELECT status, items_new, keyword_id FROM runs ORDER BY id").fetchall()
    assert runs == [("SUCCEEDED", 3, first.keyword_id), ("SUCCEEDED", 0, second.keyword_id)]


def test_single_search_lists_are_added_inactive(conn):
    import db
    search = db.ensure_search(conn, DEFAULTS.but(term="Rust Developer", location="Kochi", name="rust in Kochi"))
    assert search.keyword_id and search.location_id
    active = conn.execute("SELECT is_active FROM search_keywords WHERE id = %s", (search.keyword_id,)).fetchone()[0]
    assert active is False
    again = db.ensure_search(conn, DEFAULTS.but(term="rust developer", location="kochi", name="x"))
    assert (again.keyword_id, again.location_id) == (search.keyword_id, search.location_id)
