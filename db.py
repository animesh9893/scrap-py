"""Saves a scrape to the Supabase database the old job-scraper (Node) built, over DATABASE_URL (Postgres).

  save(...)  applies schema.sql (additive, safe to repeat), records the search in search_keywords / locations,
             upserts the jobs with upsert_jobs() (new vs seen-before), records which search found each job in
             job_search_hits and the run in runs, then links jobs to companies (link_companies(), from the old
             project) and closes jobs not seen for EXPIRE_DAYS (expire_jobs()).

Rows come from scrape.row(), shaped like public.jobs; to_db_row() turns them into what the old pipeline stored, so
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
    """A scrape.row() as the old pipeline stored it: workplace remote | hybrid | NULL (on-site or not stated),
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
    return psycopg.connect(url, sslmode=sslmode, autocommit=False)


def save(url, *, source, search_term, location, search_name, category, rows, meta, expire_days):
    """Saves one search's jobs in one transaction. Returns {"new", "updated", "linked", "closed", "run_id"}."""
    unique = list({(r["source"], r["job_id"]): r for r in rows if r["job_id"]}.values())   # one per job
    with connect(url) as conn, conn.cursor() as cur:
        with open(SCHEMA, encoding="utf-8") as f:
            cur.execute(f.read())
        cur.execute("""INSERT INTO search_keywords (keyword, category) VALUES (%s, %s)
                       ON CONFLICT ((lower(keyword))) DO UPDATE SET category = COALESCE(search_keywords.category,
                                                                                        EXCLUDED.category)
                       RETURNING id, category""", (search_term, category))
        keyword_id, keyword_category = cur.fetchone()
        cur.execute("""INSERT INTO locations (name) VALUES (%s)
                       ON CONFLICT ((lower(name))) DO UPDATE SET name = locations.name RETURNING id""", (location,))
        location_id = cur.fetchone()[0]

        error = meta.get("error")
        cur.execute("""INSERT INTO runs (search_name, actor, status, source, keyword_id, location_id)
                       VALUES (%s, 'scrap-py', 'RUNNING', %s, %s, %s) RETURNING id""",
                    (search_name, source, keyword_id, location_id))
        run_id = cur.fetchone()[0]

        db_rows = [to_db_row(r, category or keyword_category) for r in unique]
        new = 0
        for i in range(0, len(db_rows), BATCH):
            cur.execute("SELECT inserted FROM upsert_jobs(%s::jsonb)", (json.dumps(db_rows[i:i + BATCH]),))
            new += sum(inserted for (inserted,) in cur.fetchall())
        if unique:
            cur.execute("""INSERT INTO job_search_hits (source, job_id, keyword_id, location_id, run_id)
                           SELECT s, j, %s, %s, %s FROM unnest(%s::text[], %s::text[]) AS t(s, j)
                           ON CONFLICT (source, job_id, keyword_id, location_id)
                           DO UPDATE SET run_id = EXCLUDED.run_id, last_seen_at = now()""",
                        (keyword_id, location_id, run_id, [r["source"] for r in unique], [r["job_id"] for r in unique]))

        cur.execute("SELECT link_companies()")
        linked = cur.fetchone()[0]
        cur.execute("SELECT expire_jobs(%s)", (expire_days,))
        closed = cur.fetchone()[0]

        counts = meta["counts"]
        cur.execute("""UPDATE runs SET status = %s, items_found = %s, items_new = %s, items_skipped = %s, error = %s,
                                       details = %s, finished_at = now()
                       WHERE id = %s""",
                    ("FAILED" if error and not unique else "SUCCEEDED", counts["scanned"], new,
                     sum(counts["skipped"].values()), error, json.dumps(meta), run_id))
    return {"new": new, "updated": len(unique) - new, "linked": linked, "closed": closed, "run_id": run_id}
