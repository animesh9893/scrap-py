"""Saves scrapes to the Supabase database the old job-scraper (Node) built, over DATABASE_URL (Postgres).

  connect()        opens it and applies schema.sql (additive, safe to repeat)
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

import json
import os
import re

import psycopg

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
BATCH = 200      # jobs per upsert_jobs() call


def detect_category(title):
    """A category from the title when the search has none: the old project's detectCategory (src/scrape.js)."""
    t = (title or "").lower()
    if re.search(r"\b(android|ios|mobile|flutter|react native|swift|kotlin)\b", t):
        return "mobile"
    if re.search(r"full[\s-]?stack", t):
        return "fullstack"
    if re.search(r"\b(front[\s-]?end|ui engineer|ui developer|react|angular|vue)\b", t):
        return "frontend"
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


def connect(url):
    # Supabase requires TLS; set DATABASE_SSL=false for a local Postgres without it.
    sslmode = "disable" if os.environ.get("DATABASE_SSL") == "false" else "require"
    conn = psycopg.connect(url, sslmode=sslmode, autocommit=True)   # each save is its own transaction()
    with open(SCHEMA, encoding="utf-8") as f, conn.transaction():
        conn.execute(f.read())
    return conn


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
