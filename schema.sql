-- What scrap-py adds to the Supabase database that the old job-scraper (Node) created: jobs, companies, runs,
-- categories, link_companies() etc. stay as they are, so the site, its views, Telegram alerts and the LinkedIn posts
-- keep working. Everything here is additive and safe to run again: db.py runs it at the start of every save.

-- Job platforms. locations.excluded_sources and search_keywords.excluded_sources name these.
CREATE TABLE IF NOT EXISTS sources (
    id         TEXT PRIMARY KEY,                     -- linkedin | naukri | …, the same value as jobs.source
    enabled    BOOLEAN NOT NULL DEFAULT true,        -- false: never searched
    settings   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO sources (id) VALUES ('linkedin') ON CONFLICT DO NOTHING;

-- What to search for: titles and skills (frontend developer, java, react).
CREATE TABLE IF NOT EXISTS search_keywords (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    keyword               TEXT NOT NULL,
    kind                  TEXT NOT NULL DEFAULT 'title' CHECK (kind IN ('title', 'skill', 'domain')),
    category              TEXT,                      -- categories.slug (frontend, backend, …): set on the jobs it finds
    excluded_location_ids BIGINT[] NOT NULL DEFAULT '{}',   -- locations.id this keyword is never searched in
    excluded_sources      TEXT[] NOT NULL DEFAULT '{}',     -- sources.id this keyword is never searched on
    exclude_title_words   TEXT[] NOT NULL DEFAULT '{}',     -- like EXCLUDE: senior, lead, manager
    experience            TEXT[] NOT NULL DEFAULT '{}',     -- like EXPERIENCE: entry level, associate
    hours_old             INTEGER,
    max_results           INTEGER,
    interval_hours        INTEGER CHECK (interval_hours > 0),
    priority              INTEGER NOT NULL DEFAULT 100,      -- lower runs first
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS search_keywords_keyword ON search_keywords (lower(keyword));

-- Where to search.
CREATE TABLE IF NOT EXISTS locations (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             TEXT NOT NULL,                  -- Bengaluru, Pune, Remote India
    country          TEXT,                           -- IN
    aliases          TEXT[] NOT NULL DEFAULT '{}',   -- Bangalore, Bengaluru, Karnataka
    source_params    JSONB NOT NULL DEFAULT '{}',    -- how each platform names it: {"naukri": {"slug": "bangalore"}}
    excluded_sources TEXT[] NOT NULL DEFAULT '{}',   -- sources.id that are never searched here
    interval_hours   INTEGER CHECK (interval_hours > 0),
    priority         INTEGER NOT NULL DEFAULT 100,
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS locations_name ON locations (lower(name));

-- Set by expire_jobs() when a job hasn't been seen for a while; cleared when a scrape sees it again.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;

-- Which searches found each job: one job is often found by several ("react" and "frontend" in Bengaluru).
CREATE TABLE IF NOT EXISTS job_search_hits (
    source        TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    keyword_id    BIGINT NOT NULL REFERENCES search_keywords (id) ON DELETE CASCADE,
    location_id   BIGINT NOT NULL REFERENCES locations (id) ON DELETE CASCADE,
    run_id        BIGINT REFERENCES runs (id) ON DELETE SET NULL,   -- the last run that found it
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, job_id, keyword_id, location_id),
    FOREIGN KEY (source, job_id) REFERENCES jobs (source, job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS job_search_hits_search ON job_search_hits (keyword_id, location_id);

-- scrap-py's runs go in the old runs table (actor 'scrap-py'), with what it searched.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS keyword_id BIGINT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS location_id BIGINT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS details JSONB;     -- the run's meta: filters, skipped counts, warnings

-- Saves a batch of scraped jobs (a JSON array of public.jobs rows) and says which were new. A job seen again gets
-- its scraped fields refreshed (kept when the new scrape didn't get them, e.g. its page failed), keeps its first
-- category and search_name, becomes remote if any search finds it as remote, and is open again. status,
-- first_seen_at, company_slug and the tags are never touched. The rows must have distinct (source, job_id).
CREATE OR REPLACE FUNCTION upsert_jobs(items JSONB)
RETURNS TABLE (source TEXT, job_id TEXT, inserted BOOLEAN) LANGUAGE sql AS $$
    INSERT INTO jobs AS j (source, job_id, title, company, company_url, location, category, workplace, url, apply_url,
                           posted_at, employment_type, seniority, salary, description, search_name, raw)
    SELECT r.source, r.job_id, r.title, r.company, r.company_url, r.location, r.category, r.workplace, r.url,
           r.apply_url, r.posted_at, r.employment_type, r.seniority, r.salary, r.description, r.search_name, r.raw
    FROM jsonb_populate_recordset(NULL::jobs, items) r
    ON CONFLICT (source, job_id) DO UPDATE SET
        title           = COALESCE(EXCLUDED.title, j.title),
        company         = COALESCE(EXCLUDED.company, j.company),
        company_url     = COALESCE(EXCLUDED.company_url, j.company_url),
        location        = COALESCE(EXCLUDED.location, j.location),
        url             = COALESCE(EXCLUDED.url, j.url),
        apply_url       = COALESCE(EXCLUDED.apply_url, j.apply_url),
        posted_at       = COALESCE(EXCLUDED.posted_at, j.posted_at),
        employment_type = COALESCE(EXCLUDED.employment_type, j.employment_type),
        seniority       = COALESCE(EXCLUDED.seniority, j.seniority),
        salary          = COALESCE(EXCLUDED.salary, j.salary),
        description     = COALESCE(EXCLUDED.description, j.description),
        raw             = COALESCE(EXCLUDED.raw, j.raw),
        category        = COALESCE(j.category, EXCLUDED.category),
        workplace       = CASE WHEN EXCLUDED.workplace = 'remote' THEN 'remote'
                               ELSE COALESCE(j.workplace, EXCLUDED.workplace) END,
        last_seen_at    = now(),
        closed_at       = NULL
    RETURNING j.source, j.job_id, (j.xmax = 0)
$$;

-- Marks jobs no scrape has seen for `days` days as closed. Returns how many it closed.
CREATE OR REPLACE FUNCTION expire_jobs(days INTEGER) RETURNS INTEGER LANGUAGE sql AS $$
    WITH closed AS (
        UPDATE jobs SET closed_at = now()
        WHERE closed_at IS NULL AND last_seen_at < now() - make_interval(days => days)
        RETURNING 1)
    SELECT COUNT(*)::int FROM closed
$$;

-- Each company's jobs: open now, first seen in the last 7 / 30 days, and the latest one.
CREATE OR REPLACE VIEW company_stats AS
    SELECT c.slug,
           (COUNT(j.job_id) FILTER (WHERE j.closed_at IS NULL AND j.status <> 'skipped'))::int AS active_jobs,
           (COUNT(j.job_id) FILTER (WHERE j.first_seen_at > now() - interval '7 days'))::int AS jobs_week,
           (COUNT(j.job_id) FILTER (WHERE j.first_seen_at > now() - interval '30 days'))::int AS jobs_month,
           MAX(j.first_seen_at) AS last_job_at,
           COUNT(DISTINCT j.source)::int AS sources
    FROM companies c LEFT JOIN jobs j ON j.company_slug = c.slug
    GROUP BY c.slug;

-- The company's logo on each job (LinkedIn's, from the job page), as a column for automations that read jobs.
-- companies.logo_url holds the newest one per company. LinkedIn's image links expire after some weeks.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_logo TEXT GENERATED ALWAYS AS (raw->>'companyLogo') STORED;

-- The site's job list (base.sql has the first version), plus the company logo and "places": the names of the
-- locations (locations.name) whose searches found the job, for the site's location filter.
CREATE OR REPLACE VIEW public_jobs AS
    SELECT j.job_id AS id, j.title, j.company, j.location, j.category, j.workplace, j.url,
           j.posted_at AS "postedAt", j.employment_type AS "employmentType", j.seniority,
           j.first_seen_at AS "firstSeenAt", j.company_slug AS "companySlug",
           COALESCE(c.is_premium, false) AS "companyPremium", j.role, j.level, j.tags,
           COALESCE(c.logo_url, j.raw->>'companyLogo') AS "companyLogo",
           ARRAY(SELECT DISTINCT l.name FROM job_search_hits h JOIN locations l ON l.id = h.location_id
                 WHERE h.source = j.source AND h.job_id = j.job_id ORDER BY l.name) AS places
    FROM jobs j LEFT JOIN companies c ON c.slug = j.company_slug
    WHERE j.status <> 'skipped' AND j.first_seen_at > now() - interval '30 days';
CREATE INDEX IF NOT EXISTS job_search_hits_job ON job_search_hits (source, job_id);

-- The site's location filter: active locations, grouped by country, with how many listed jobs each has.
CREATE OR REPLACE VIEW public_locations AS
    SELECT l.name, l.country, l.priority,
           (SELECT COUNT(DISTINCT (h.source, h.job_id))::int FROM job_search_hits h
            JOIN jobs j ON j.source = h.source AND j.job_id = h.job_id
            WHERE h.location_id = l.id AND j.status <> 'skipped' AND j.first_seen_at > now() - interval '30 days') AS jobs
    FROM locations l WHERE l.is_active;

-- Private, like the old tables: RLS with no policies, and the site's public key can't read them.
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_keywords ENABLE ROW LEVEL SECURITY;
ALTER TABLE locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_search_hits ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON sources, search_keywords, locations, job_search_hits, company_stats FROM anon, authenticated;
        REVOKE EXECUTE ON FUNCTION upsert_jobs(JSONB), expire_jobs(INTEGER) FROM PUBLIC, anon, authenticated;
        GRANT SELECT ON public_jobs, public_locations TO anon, authenticated;
    END IF;
END $$;
