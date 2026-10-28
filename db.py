"""Saves scrapes to the Supabase database the old job-scraper (Node) built, over DATABASE_URL (Postgres).

  connect()        opens it and applies base.sql (the old project's tables, for a fresh database) and schema.sql
                   (additive), both safe to repeat, when they changed since the last run (apply_schema())
  known_job_ids()  ids of jobs saved and seen lately, whose job pages a scrape can skip
  ensure_search()  for a search from the environment: its search_keywords / locations rows (added inactive, so a
                   one-off search doesn't join the schedule)
  save()           one search, in its own transaction: upserts the jobs with upsert_jobs() (new vs seen before),
                   marks the known jobs it found again as seen, records which search found each job in
                   job_search_hits and the run in runs
  import_board_companies()  adds the companies in data/ats-companies.csv, with their job boards (Ashby), to companies
  due_boards()     the companies whose job board is due a read (boards.py)
  save_board()     one board's jobs: upserts them, points them at the company, closes the ones the board dropped
  finish()         links jobs to companies (link_companies(), from the old project), closes jobs not seen for
                   EXPIRE_DAYS (expire_jobs()) and re-reads the years of experience of saved jobs read by an older
                   experience.py (reparse_experience())

Rows come from the sources, shaped like public.jobs; to_db_row() turns them into what the old pipeline stored, so
the site's views, Telegram alerts and LinkedIn posts read them like any other job.
"""

import csv
import hashlib
import json
import logging
import os
import re
import time

import psycopg

import experience

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = [os.path.join(HERE, "base.sql"), os.path.join(HERE, "schema.sql"),   # the old project's tables, then ours,
          os.path.join(HERE, "premium.sql")]                                       # then the premium companies
BOARDS_CSV = os.path.join(HERE, "data", "ats-companies.csv")   # companies with a job board, imported after the SQL
BATCH = 200      # jobs per upsert_jobs() call
REPARSE = 3000   # jobs per run whose years of experience reparse_experience() re-reads

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
    for path in SCHEMA + [BOARDS_CSV]:
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
        import_board_companies(conn)
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


def read_boards_csv(path=BOARDS_CSV):
    """data/ats-companies.csv (company, careers_page, website, main_country, open_jobs, ats) as dicts for
    import_board_companies(): one per board, the boards it can read (jobs.ashbyhq.com/<board> for now)."""
    from sources import ashby
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        ats, name = (r.get("ats") or "").strip().lower(), (r.get("company") or "").strip()
        board = ashby.board_slug(r.get("careers_page")) if ats == "ashby" else None
        if not board or not name or (ats, board.lower()) in out:
            continue
        jobs = (r.get("open_jobs") or "").strip()
        out[(ats, board.lower())] = {"name": name, "ats": ats, "board": board,
                                     "careers_url": f"https://jobs.ashbyhq.com/{board}",
                                     "website": (r.get("website") or "").strip() or None,
                                     "country": (r.get("main_country") or "").strip().upper() or None,
                                     "open_jobs": int(jobs) if jobs.isdigit() else 0}
    return list(out.values())


def import_board_companies(conn, path=BOARDS_CSV):
    """Adds the CSV's companies with their job boards. A board already in companies gets its link, site and country
    refreshed. Else a company of the same name (company_key(), as link_companies() matches) gets the board, so the
    LinkedIn jobs and the board's jobs share one company; else the company is added. Of several boards whose names
    match (three different "Tempo"s), the one with the most jobs gets the name, so LinkedIn jobs link to it; the
    others are added without name_keys. Returns how many companies it added."""
    items = json.dumps(read_boards_csv(path))
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS board_import (name TEXT, ats TEXT, board TEXT, careers_url TEXT, website TEXT,
                                                     country TEXT, open_jobs INT, key TEXT, domain TEXT, first BOOLEAN);
        TRUNCATE board_import""")
    conn.execute("""
        INSERT INTO board_import
        SELECT name, ats, board, careers_url, website, country, open_jobs, company_key(name),
               NULLIF(lower(substring(website FROM '^https?://(?:www\\.)?([^/:?#]+)')), ''),
               row_number() OVER (PARTITION BY company_key(name) ORDER BY open_jobs DESC, board) = 1
        FROM jsonb_to_recordset(%s::jsonb)
             AS x(name TEXT, ats TEXT, board TEXT, careers_url TEXT, website TEXT, country TEXT, open_jobs INT)""",
                 (items,))
    conn.execute("""
        UPDATE companies c SET careers_url = i.careers_url, website = i.website, country = i.country,
                               domain = COALESCE(c.domain, i.domain)
        FROM board_import i WHERE c.ats = i.ats AND lower(c.ats_slug) = lower(i.board)""")
    # Existing companies by name: each board to one company and each company to one board.
    conn.execute("""
        UPDATE companies c SET ats = m.ats, ats_slug = m.board, careers_url = m.careers_url, website = m.website,
                               country = m.country, domain = COALESCE(c.domain, m.domain)
        FROM (SELECT DISTINCT ON (slug) * FROM (
                  SELECT DISTINCT ON (i.ats, i.board) i.*, c.slug
                  FROM board_import i JOIN companies c ON i.key = ANY (c.name_keys) AND c.ats_slug IS NULL
                  WHERE i.first AND NOT EXISTS (SELECT 1 FROM companies o
                                                WHERE o.ats = i.ats AND lower(o.ats_slug) = lower(i.board))
                  ORDER BY i.ats, i.board, c.is_premium DESC, c.slug) t
              ORDER BY slug, open_jobs DESC) m
        WHERE c.slug = m.slug""")
    added = 0
    # The rest are new: slugged from the name, then name-board when that is taken, then board-ats.
    for slug in ("base", "base || '-' || lower(i.board)", "lower(i.board) || '-' || i.ats"):
        added += conn.execute(f"""
            INSERT INTO companies (slug, name, name_keys, domain, ats, ats_slug, careers_url, website, country)
            SELECT {slug}, i.name, CASE WHEN i.first AND i.key IS NOT NULL THEN ARRAY[i.key] ELSE '{{}}' END,
                   i.domain, i.ats, i.board, i.careers_url, i.website, i.country
            FROM (SELECT *, trim(BOTH '-' FROM regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g')) AS base
                  FROM board_import) i
            WHERE i.base <> '' AND NOT EXISTS (SELECT 1 FROM companies o
                                               WHERE o.ats = i.ats AND lower(o.ats_slug) = lower(i.board))
            ON CONFLICT DO NOTHING""").rowcount
    return added


def due_boards(conn, ats, interval_hours, limit=None, board=None):
    """Companies whose job board wasn't read in the last `interval_hours` (never read first, then the longest ago),
    as (slug, name, board). Boards that weren't found are tried again weekly. `board`: only that one, due or not."""
    if board:
        return conn.execute("SELECT slug, name, ats_slug FROM companies WHERE ats = %s AND lower(ats_slug) = lower(%s)",
                            (ats, board)).fetchall()
    return conn.execute(
        """SELECT slug, name, ats_slug FROM companies
           WHERE ats = %s AND ats_slug IS NOT NULL
             AND (board_checked_at IS NULL OR board_checked_at < now() - CASE WHEN board_status = 'not_found'
                  THEN interval '7 days' ELSE make_interval(hours => %s) END)
           ORDER BY board_checked_at NULLS FIRST, slug LIMIT %s""", (ats, interval_hours, limit)).fetchall()


def save_board(conn, source, company_slug, rows, counts, status="ok", error=None):
    """One board's read, in its own transaction: upserts its jobs (rows from sources.<ats>.rows()), points them at
    the company, closes its saved jobs the board no longer lists, and records the read on the company. A board that
    failed (status error / not_found) only gets that recorded. Returns {"new", "updated", "closed"}."""
    unique = list({r["job_id"]: r for r in rows}.values())
    new = closed = 0
    with conn.transaction():
        if status == "ok":
            db_rows = [to_db_row(r, None) for r in unique]
            for i in range(0, len(db_rows), BATCH):
                cur = conn.execute("SELECT inserted FROM upsert_jobs(%s::jsonb)", (json.dumps(db_rows[i:i + BATCH]),))
                new += sum(inserted for (inserted,) in cur.fetchall())
            ids = [r["job_id"] for r in unique]
            conn.execute("UPDATE jobs SET company_slug = %s WHERE source = %s AND job_id = ANY(%s)",
                         (company_slug, source, ids))
            closed = conn.execute("""UPDATE jobs SET closed_at = now()
                                     WHERE source = %s AND company_slug = %s AND closed_at IS NULL
                                       AND NOT (job_id = ANY(%s))""", (source, company_slug, ids)).rowcount
        conn.execute("""UPDATE companies SET board_checked_at = now(), board_status = %s, board_error = %s,
                                             board_jobs = COALESCE(%s, board_jobs), board_kept = COALESCE(%s, board_kept)
                        WHERE slug = %s""",
                     (status, error, counts.get("listed") if status == "ok" else None,
                      counts.get("kept") if status == "ok" else None, company_slug))
    return {"new": new, "updated": len(unique) - new, "closed": closed}


def record_run(conn, source, name, meta, error=None):
    """A boards.py run in the runs table, with its counts in details."""
    conn.execute("""INSERT INTO runs (search_name, actor, status, source, items_found, items_new, error, details,
                                      finished_at)
                    VALUES (%s, 'scrap-py', %s, %s, %s, %s, %s, %s, now())""",
                 (name, "FAILED" if error else "SUCCEEDED", source, meta.get("listed", 0), meta.get("new", 0), error,
                  json.dumps(meta)))


def reparse_experience(conn, limit=REPARSE):
    """Re-reads raw.experience_years from the saved title and description of jobs read by an older experience.py
    (raw.experience_parser below experience.VERSION), up to `limit` a run, so a change there reaches saved jobs
    over the next runs. Returns how many it re-read."""
    rows = conn.execute(
        """SELECT source, job_id, title, description,
                  concat_ws(E'\n', raw->'sections'->>'requirements', raw->'sections'->>'qualifications')
           FROM jobs WHERE raw IS NOT NULL AND COALESCE((raw->>'experience_parser')::int, 0) < %s
           LIMIT %s""", (experience.VERSION, limit)).fetchall()
    updates = [(json.dumps({"experience_years": experience.years(title, asks, description),
                            "experience_parser": experience.VERSION}), source, job_id)
               for source, job_id, title, description, asks in rows]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("UPDATE jobs SET raw = raw || %s::jsonb WHERE source = %s AND job_id = %s", updates)
    return len(updates)


def finish(conn, expire_days):
    """After a run's searches. Returns {"linked", "closed", "reparsed"}."""
    with conn.transaction():
        linked = conn.execute("SELECT link_companies()").fetchone()[0]
        closed = conn.execute("SELECT expire_jobs(%s)", (expire_days,)).fetchone()[0]
    return {"linked": linked, "closed": closed, "reparsed": reparse_experience(conn)}
