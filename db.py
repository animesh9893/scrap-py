"""Saves scrapes to the Supabase database the old job-scraper (Node) built, over DATABASE_URL (Postgres).

  connect()        opens it and applies base.sql (the old project's tables, for a fresh database) and schema.sql
                   (additive), both safe to repeat, when they changed since the last run (apply_schema())
  known_job_ids()  ids of jobs saved and seen lately, whose job pages a scrape can skip
  ensure_search()  for a search from the environment: its search_keywords / locations rows (added inactive, so a
                   one-off search doesn't join the schedule)
  save()           one search, in its own transaction: upserts the jobs with upsert_jobs() (new vs seen before),
                   marks the known jobs it found again as seen, records which search found each job in
                   job_search_hits and the run in runs
  finish()         links jobs to companies (link_companies(), from the old project) and closes jobs not seen for
                   EXPIRE_DAYS (expire_jobs())

Rows come from the sources, shaped like public.jobs; to_db_row() turns them into what the old pipeline stored, so
the site's views, Telegram alerts and LinkedIn posts read them like any other job.
"""

import hashlib
import json
import logging
import os
import re
import time

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = [os.path.join(HERE, "base.sql"), os.path.join(HERE, "schema.sql")]   # the old project's tables, then ours
BATCH = 200      # jobs per upsert_jobs() call

log = logging.getLogger(__name__)


def detect_category(title):
    """A category from the title when the search has none: the old project's detectCategory (src/scrape.js), plus
    the categories added since (ai, data, devops, security, qa)."""
    t = (title or "").lower()
    if re.search(r"\b(android|ios|mobile|flutter|react native|swift|kotlin)\b", t):
        return "mobile"
    if re.search(r"full[\s-]?stack", t):
        return "fullstack"
    if re.search(r"\b(front[\s-]?end|ui engineer|ui developer|react|angular|vue)\b", t):
        return "frontend"
    if re.search(r"\b(machine learning|ml|ai|deep learning|llm|nlp|computer vision|gen ?ai|mlops)\b", t):
        return "ai"
    if re.search(r"\b(data (engineer|scientist|science|analyst|analytics)|analytics engineer|big data|etl|bi developer|"
                 r"power bi|databricks)\b", t):
        return "data"
    if re.search(r"\b(devops|sre|site reliability|platform engineer|cloud (engineer|architect)|kubernetes|"
                 r"infrastructure engineer|devsecops)\b", t):
        return "devops"
    if re.search(r"\b(security|cyber ?security|appsec|penetration|soc analyst)\b", t):
        return "security"
    if re.search(r"\b(qa|quality assurance|test(ing)? engineer|sdet|automation test(er|ing)?|tester)\b", t):
        return "qa"
    if re.search(r"\b(back[\s-]?end|java|golang|node(\.?js)?|python|django|spring)\b|\.net\b|c#", t):
        return "backend"
    return None


def to_db_row(r, category):
    """A source's row as the old pipeline stored it: workplace remote | hybrid | NULL (on-site or not stated),
    company_slug left for link_companies() (it must name a companies row), and raw.companyLogo, which
    link_companies() copies to the company's logo."""
    db = {k: v for k, v in r.items() if k not in ("company_slug", "last_seen_at")}
    db["workplace"] = {"Remote": "remote", "Hybrid": "hybrid"}.get(r["workplace"])
    db["category"] = category or detect_category(r["title"])
    db["raw"] = {**r["raw"], "companyLogo": r["raw"].get("company_logo")}
    return db


def schema_digest():
    h = hashlib.sha256()
    for path in SCHEMA:
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def apply_schema(conn):
    """Applies base.sql and schema.sql when they changed since the last run that did. Runs start at the same time
    (the schedule, a sweep, the posts workflow, pushes in the other repo), and DDL from two of them at once
    deadlocks, so they take turns on an advisory lock, and only the first after a change runs the SQL."""
    digest = schema_digest()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('scrap-py schema'))")
        if conn.execute("SELECT to_regclass('public.scrap_py_schema') IS NOT NULL").fetchone()[0] and \
                conn.execute("SELECT 1 FROM scrap_py_schema WHERE digest = %s", (digest,)).fetchone():
            return False
        for path in SCHEMA:
            with open(path, encoding="utf-8") as f:
                conn.execute(f.read())
        conn.execute("""CREATE TABLE IF NOT EXISTS scrap_py_schema (
                            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), digest TEXT NOT NULL,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
                        ALTER TABLE scrap_py_schema ENABLE ROW LEVEL SECURITY""")
        conn.execute("""INSERT INTO scrap_py_schema (id, digest) VALUES (1, %s)
                        ON CONFLICT (id) DO UPDATE SET digest = EXCLUDED.digest, applied_at = now()""", (digest,))
    return True


def connect(url):
    # Supabase requires TLS; set DATABASE_SSL=false for a local Postgres without it.
    sslmode = "disable" if os.environ.get("DATABASE_SSL") == "false" else "require"
    conn = psycopg.connect(url, sslmode=sslmode, autocommit=True)   # each save is its own transaction()
    for attempt in range(3):
        try:
            if apply_schema(conn):
                log.info("Applied base.sql and schema.sql")
            break
        except psycopg.errors.DeadlockDetected:
            # Still possible against a run of older code, which applies the SQL every time without the lock.
            if attempt == 2:
                raise
            log.warning("Deadlock applying the schema: trying again")
            time.sleep(5 * (attempt + 1))
    return conn


def alive(conn, url):
    """conn, or a new connection when it has dropped."""
    try:
        conn.execute("SELECT 1")
        return conn
    except psycopg.OperationalError:
        log.warning("Database connection dropped: reconnecting")
        try:
            conn.close()
        except Exception:
            pass
        return connect(url)


def known_job_ids(conn, source, days):
    """Ids of the source's jobs seen in the last `days` days: a scrape finding them again only marks them seen."""
    rows = conn.execute("SELECT job_id FROM jobs WHERE source = %s AND last_seen_at > now() - make_interval(days => %s)",
                        (source, days)).fetchall()
    return frozenset(job_id for (job_id,) in rows)


def ensure_search(conn, search):
    """The search with its keyword_id / location_id, adding the keyword and location (inactive) the first time."""
    with conn.transaction():
        keyword_id, category = conn.execute(
            """INSERT INTO search_keywords (keyword, category, is_active) VALUES (%s, %s, false)
               ON CONFLICT ((lower(keyword))) DO UPDATE SET category = COALESCE(search_keywords.category,
                                                                                EXCLUDED.category)
               RETURNING id, category""", (search.term, search.category)).fetchone()
        location_id, params = conn.execute(
            """INSERT INTO locations (name, is_active) VALUES (%s, false)
               ON CONFLICT ((lower(name))) DO UPDATE SET name = locations.name RETURNING id, source_params""",
            (search.location,)).fetchone()
    return search.but(keyword_id=keyword_id, location_id=location_id, category=search.category or category,
                      source_location=search.source_location or ((params or {}).get(search.source) or {}).get("location"))


def save(conn, search, result):
    """Saves one search's result. Returns {"new", "updated", "seen", "run_id"}."""
    meta, error = result.meta, result.meta.get("error")
    unique = list({(r["source"], r["job_id"]): r for r in result.rows if r["job_id"]}.values())   # one per job
    with conn.transaction():
        run_id = conn.execute(
            """INSERT INTO runs (search_name, actor, status, source, keyword_id, location_id)
               VALUES (%s, 'scrap-py', 'RUNNING', %s, %s, %s) RETURNING id""",
            (search.name, search.source, search.keyword_id, search.location_id)).fetchone()[0]

        new = 0
        db_rows = [to_db_row(r, search.category) for r in unique]
        for i in range(0, len(db_rows), BATCH):
            cur = conn.execute("SELECT inserted FROM upsert_jobs(%s::jsonb)", (json.dumps(db_rows[i:i + BATCH]),))
            new += sum(inserted for (inserted,) in cur.fetchall())
        seen = 0
        if result.seen:
            seen = conn.execute("""UPDATE jobs SET last_seen_at = now(), closed_at = NULL
                                   WHERE source = %s AND job_id = ANY(%s)""",
                                (search.source, list(result.seen))).rowcount

        ids = [r["job_id"] for r in unique] + list(result.seen)
        if ids and search.keyword_id and search.location_id:
            conn.execute("""INSERT INTO job_search_hits (source, job_id, keyword_id, location_id, run_id)
                            SELECT %s, j.job_id, %s, %s, %s FROM jobs j WHERE j.source = %s AND j.job_id = ANY(%s)
                            ON CONFLICT (source, job_id, keyword_id, location_id)
                            DO UPDATE SET run_id = EXCLUDED.run_id, last_seen_at = now()""",
                         (search.source, search.keyword_id, search.location_id, run_id, search.source, ids))

        counts = meta["counts"]
        conn.execute("""UPDATE runs SET status = %s, items_found = %s, items_new = %s, items_skipped = %s, error = %s,
                                        details = %s, finished_at = now()
                        WHERE id = %s""",
                     ("FAILED" if error and not unique and not result.seen else "SUCCEEDED", counts["scanned"], new,
                      sum(counts["skipped"].values()), error, json.dumps(meta), run_id))
    return {"new": new, "updated": len(unique) - new, "seen": seen, "run_id": run_id}


def finish(conn, expire_days):
    """After a run's searches. Returns {"linked", "closed"}."""
    with conn.transaction():
        linked = conn.execute("SELECT link_companies()").fetchone()[0]
        closed = conn.execute("SELECT expire_jobs(%s)", (expire_days,)).fetchone()[0]
    return {"linked": linked, "closed": closed}
