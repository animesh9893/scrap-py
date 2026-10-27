"""db.py and planner.plan() against a real Postgres (an empty one is fine: db.connect() creates the tables).
Skipped unless TEST_DATABASE_URL is set; the test empties the tables it uses, so never point it at Supabase."""

import json
import os
from datetime import datetime, timedelta, timezone

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
    with open(os.path.join(db.HERE, "seed.sql"), encoding="utf-8") as f:
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
    pairs = lists["keywords"] * lists["locations"]          # seed.sql has no exclusions
    assert lists["keywords"] > 1 and lists["locations"] > 1 and (due, len(planned)) == (pairs, 2)

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
    page = conn.execute("SELECT name, url FROM public_job_hiring LIMIT 1").fetchone()
    assert page == ("Priya Sharma", "https://www.linkedin.com/in/priya-sharma-1a2b3c")

    # The pair just run isn't due any more.
    planned, due, _ = plan(conn, max_tasks=100)
    assert due == pairs - 1 and first.name not in [p.search.name for p in planned]

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


def test_schema_applied_once_and_runs_take_turns(conn):
    import threading

    import db
    conn.execute("DELETE FROM scrap_py_schema")
    assert db.apply_schema(conn) is True        # changed (no digest yet): applied
    assert db.apply_schema(conn) is False       # unchanged: skipped

    # Runs starting together after a change: one applies the SQL, the others wait for it and skip.
    conn.execute("DELETE FROM scrap_py_schema")
    results, errors = [], []

    def start():
        try:
            c = db.connect(URL)
            results.append(c.execute("SELECT digest FROM scrap_py_schema").fetchone()[0])
            c.close()
        except Exception as e:      # a deadlock would land here
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results == [db.schema_digest()] * 4


TITLES = {   # real titles from the jobs table: in a post or not
    "Software Development Engineer - II, FinTech - Machine Learning": True,
    "Software Engineer III - Java Full Stack Developer + GenAI / LLM + AWS": True,
    "Applications Development Intermediate Programmer Analyst": True,
    "Data Scientist": True,
    "SDE II, ShipTech/Visibility": True,
    "Salesforce Developer": True,
    "Senior Software Engineer": False,
    "Sr. Java Developer": False,
    "Lead Data Engineer": False,
    "Staff Engineer, Platform": False,
    "AI Operations Engineering Technical Leader": False,
    "Engineering Manager": False,
    "Python Developer Assistant Vice President": False,
    "Customer Engineer, Cloud AI, Financial Services, Google Cloud": False,
    "Technical Support Engineer": False,
    "Business Analyst (4 to 6 Years) - Lending": False,
    "Oracle Fusion Accounting Hub – Consultant, Functional Transformation": False,
    "Technical Engineer Contractor": False,
    "Software Engineering Intern": False,
    "Sales Engineer": False,
    "Recruiter": False,
    "Full-stack Engineer 5": False,
    "Data Engineer IV": False,
    "Advisory Data Scientist - IBM Quantum": False,
    "Software Engineer-II": True,
}


def test_post_job_ok_keeps_entry_and_mid_software_roles(conn):
    got = {t: conn.execute("SELECT post_job_ok(%s, 'Mid-Senior level')", (t,)).fetchone()[0] for t in TITLES}
    assert got == TITLES
    assert conn.execute("SELECT post_job_ok('Software Engineer', 'Director')").fetchone()[0] is False
    assert conn.execute("SELECT post_job_ok('Software Engineer', NULL)").fetchone()[0] is True


def test_posts_take_only_tier_1_companies_with_a_real_logo(conn):
    import db
    logo = "https://media.licdn.com/dms/image/x/logo.png"
    conn.execute("""INSERT INTO companies (slug, name, logo_url) VALUES
                        ('google', 'Google', %s), ('infosys', 'Infosys', %s), ('meta', 'Meta', 'https://static.licdn.com/ghost.png'),
                        ('tiny-startup', 'Tiny Startup', %s)""", (logo, logo, logo))
    with open(os.path.join(db.HERE, "premium.sql"), encoding="utf-8") as f:
        conn.execute(f.read())
    tiers = dict(conn.execute("SELECT slug, premium_tier FROM companies WHERE is_premium").fetchall())
    assert tiers == {"google": 1, "infosys": 2, "meta": 1}
    assert conn.execute("SELECT hashtag FROM companies WHERE slug = 'google'").fetchone()[0] == "google"

    for i, (slug, title) in enumerate([("google", "Software Engineer II"), ("google", "Senior Software Engineer"),
                                       ("google", "Technical Support Engineer"), ("infosys", "Java Developer"),
                                       ("meta", "Software Engineer"), ("tiny-startup", "Backend Developer")]):
        conn.execute("""INSERT INTO jobs (source, job_id, title, company, company_slug, seniority)
                        VALUES ('linkedin', %s, %s, %s, %s, 'Mid-Senior level')""", (str(i), title, slug, slug))
    rows = conn.execute("SELECT company_slug, title FROM post_candidates").fetchall()
    assert rows == [("google", "Software Engineer II")]

    # A company taken off the list loses its tier the next time premium.sql runs.
    conn.execute("UPDATE companies SET premium_tier = 1, is_premium = true WHERE slug = 'tiny-startup'")
    conn.execute(open(os.path.join(db.HERE, "premium.sql"), encoding="utf-8").read())
    assert conn.execute("SELECT premium_tier, is_premium FROM companies WHERE slug = 'tiny-startup'").fetchone() == (None, False)


def test_post_first_comment_is_read_through_the_posts_view(conn):
    post_id = conn.execute("INSERT INTO posts (person, body, first_comment) VALUES ('animesh', 'b', 'links here') RETURNING id").fetchone()[0]
    assert conn.execute('SELECT "firstComment" FROM public_linkedin_posts WHERE id = %s', (post_id,)).fetchone()[0] == "links here"


def test_delete_old_jobs_keeps_fresh_and_recently_posted_ones(conn):
    conn.execute("TRUNCATE posts, post_jobs")
    hours = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="minutes")
    day = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).date().isoformat()
    jobs = {"fresh": hours(5), "old": hours(50), "yesterday": day(1), "day_old": day(3), "no_date": None,
            "old_in_post": hours(60)}
    for job_id, posted in jobs.items():
        conn.execute("INSERT INTO jobs (source, job_id, posted_at, first_seen_at) VALUES ('linkedin', %s, %s, %s)",
                     (job_id, posted, datetime.now(timezone.utc) - timedelta(hours=72 if job_id != "no_date" else 1)))
    post_id = conn.execute("INSERT INTO posts (person, body) VALUES ('animesh', 'b') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO post_jobs (post_id, source, job_id, position) VALUES (%s, 'linkedin', 'old_in_post', 1)",
                 (post_id,))

    assert conn.execute("SELECT delete_old_jobs()").fetchone()[0] == 2
    left = {r[0] for r in conn.execute("SELECT job_id FROM jobs").fetchall()}
    assert left == {"fresh", "yesterday", "no_date", "old_in_post"}


def test_reparse_experience_and_the_board_filter_columns(conn):
    import db
    import experience
    raw = lambda years, parser: {"experience_years": years, "experience_parser": parser,
                                 "sections": {"requirements": "* 3-5 years of Java experience"}}
    rows = [("old", "We have served clients for over 25 years.\n* 3-5 years of Java experience", raw({"min": 25, "max": None}, None)),
            ("current", "* 2+ years of Go", raw({"min": 9, "max": None}, experience.VERSION)),
            ("none", "Build things with us.", raw({"min": 4, "max": None}, 1))]
    for job_id, description, r in rows:
        conn.execute("""INSERT INTO jobs (source, job_id, title, description, raw, first_seen_at)
                        VALUES ('linkedin', %s, 'Engineer', %s, %s::jsonb, now())""", (job_id, description, json.dumps(r)))

    assert db.reparse_experience(conn) == 2
    assert db.reparse_experience(conn) == 0      # all read by this experience.py now
    got = dict((r[0], r[1:]) for r in conn.execute(
        """SELECT id, "experienceMin", "experienceMax", "experienceFrom", "experienceTo" FROM public_jobs""").fetchall())
    assert got == {"old": (3, 5, 3, 5), "current": (9, None, 9, 9), "none": (None, None, 0, 99)}
