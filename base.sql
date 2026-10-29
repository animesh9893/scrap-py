-- The old job-scraper's database (its src/db.js SCHEMA, as of commit 01e957f), so scrap-py also works on a fresh
-- Supabase project: jobs, categories, companies, salaries, runs, the company-linking functions, the views the site
-- reads, and the tables the old Node steps use (posts, Telegram, Apify tokens). Every statement is IF NOT EXISTS /
-- OR REPLACE, so on the old database it changes nothing. db.py runs it before schema.sql on every save; keep it in
-- step with the Node project if that changes its schema.

CREATE TABLE IF NOT EXISTS jobs (
    source          TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    title           TEXT,
    company         TEXT,
    company_url     TEXT,
    location        TEXT,
    category        TEXT,                        -- from the search: frontend | backend | fullstack | mobile
    workplace       TEXT,                        -- remote | hybrid | NULL (on-site / not stated)
    url             TEXT,
    apply_url       TEXT,
    posted_at       TEXT,
    employment_type TEXT,
    seniority       TEXT,
    salary          TEXT,
    description     TEXT,
    search_name     TEXT,
    raw             JSONB,                       -- full Actor item
    status          TEXT NOT NULL DEFAULT 'new', -- new → drafted → posted / skipped
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, job_id)
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

-- Columns added after the first release; no-ops on new databases.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workplace TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_slug TEXT;   -- companies.slug, set by link_companies()
CREATE INDEX IF NOT EXISTS jobs_company_slug ON jobs(company_slug);
-- Keyword tags from the title and description (src/tags.js), set after each scrape.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS role TEXT;          -- frontend | backend | mobile | data | ml | devops | …
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS level TEXT;         -- intern | junior | mid | senior | lead | staff | principal
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tags TEXT[];        -- tech named, e.g. {React,TypeScript}
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tagged_at TIMESTAMPTZ;
UPDATE jobs SET category = split_part(search_name, '-', 1)
WHERE category IS NULL AND split_part(search_name, '-', 1) IN ('frontend', 'backend', 'fullstack', 'mobile');

-- Tags shown as tabs on the site; searches.category points here.
CREATE TABLE IF NOT EXISTS categories (
    slug       TEXT PRIMARY KEY,                 -- e.g. frontend
    label      TEXT NOT NULL,                    -- e.g. Frontend
    sort_order INTEGER NOT NULL DEFAULT 100      -- tab order, lowest first
);

-- One row per Actor run to do. Each scrape takes the few searches that ran longest ago
-- (config searchesPerRun), so a limited Apify budget rotates through all of them.
CREATE TABLE IF NOT EXISTS searches (
    name        TEXT PRIMARY KEY,                -- e.g. backend-remote
    category    TEXT REFERENCES categories (slug) ON UPDATE CASCADE,
    workplace   TEXT CHECK (workplace IN ('remote', 'hybrid')), -- marks every job it finds
    input       JSONB NOT NULL DEFAULT '{}',     -- Actor input, merged over config defaults
    is_active   BOOLEAN NOT NULL DEFAULT true,
    priority    INTEGER NOT NULL DEFAULT 100,    -- lower runs first among equally stale searches
    last_run_at TIMESTAMPTZ,
    last_status TEXT,                            -- SUCCEEDED | FAILED
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE searches ENABLE ROW LEVEL SECURITY;
-- Set: the search runs every N hours on top of the rotation (premium company searches). NULL: it rotates.
ALTER TABLE searches ADD COLUMN IF NOT EXISTS interval_hours INTEGER CHECK (interval_hours > 0);

-- One row per employer. Premium ones are seeded (src/seed-data.js) and searched by LinkedIn company ID;
-- every other company a job mentions is added by link_companies().
CREATE TABLE IF NOT EXISTS companies (
    slug           TEXT PRIMARY KEY,                -- e.g. google; used in the site URL
    name           TEXT NOT NULL,
    linkedin_id    TEXT,                            -- numeric id for LinkedIn's f_C filter
    linkedin_slug  TEXT UNIQUE,                     -- linkedin.com/company/<this>, matched against jobs.company_url
    name_keys      TEXT[] NOT NULL DEFAULT '{}',    -- company_key() of the name and aliases, matched against jobs.company
    domain         TEXT,                            -- fallback logo (favicon) when there's no logo_url
    logo_url       TEXT,                            -- from its LinkedIn jobs
    employees      INTEGER,                         -- from its LinkedIn page
    linkedin_checked_at TIMESTAMPTZ,                -- when its LinkedIn page was last read for the ID
    is_premium     BOOLEAN NOT NULL DEFAULT false,
    premium_source TEXT CHECK (premium_source IN ('default', 'earned')),
    sort_order     INTEGER NOT NULL DEFAULT 100,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS companies_name_keys ON companies USING gin (name_keys);
ALTER TABLE companies ENABLE ROW LEVEL SECURITY;
-- The companies LinkedIn posts feature, set by premium.sql: 1 = the big names posts are made of, 2 = big IT
-- services and Big 4 (premium on the site, never in posts). hashtag: the brand's own tag (#google), for posts.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS premium_tier SMALLINT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS hashtag TEXT;

-- Glassdoor salaries, one row per job title, cached per company for salaries.refreshDays (config).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS salaries_fetched_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS salaries_status TEXT;     -- ok | none | not_found | mismatch | error
ALTER TABLE companies ADD COLUMN IF NOT EXISTS salaries_error TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS glassdoor_name TEXT;      -- the employer Glassdoor matched the name to
ALTER TABLE companies ADD COLUMN IF NOT EXISTS glassdoor_rating NUMERIC;
CREATE TABLE IF NOT EXISTS company_salaries (
    company_slug   TEXT NOT NULL REFERENCES companies(slug) ON DELETE CASCADE ON UPDATE CASCADE,
    job_title      TEXT NOT NULL,       -- as Glassdoor names it
    query          TEXT NOT NULL,       -- the role (salaries.roles) it was found by
    currency       TEXT NOT NULL,
    pay_period     TEXT NOT NULL,       -- what Glassdoor reported; the amounts below are per year
    min_annual     NUMERIC,             -- total pay, 10th percentile
    median_annual  NUMERIC,
    max_annual     NUMERIC,             -- 90th percentile
    usd_per_unit   NUMERIC,             -- exchange rate used for the *_usd columns
    min_usd        NUMERIC,
    median_usd     NUMERIC,
    max_usd        NUMERIC,
    location       TEXT,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_slug, job_title)
);
ALTER TABLE company_salaries ENABLE ROW LEVEL SECURITY;
-- USD value of one unit of each currency, refreshed at most daily (Frankfurter, ECB rates).
CREATE TABLE IF NOT EXISTS fx_rates (
    currency      TEXT PRIMARY KEY,
    usd_per_unit  NUMERIC NOT NULL,
    rate_date     DATE,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE fx_rates ENABLE ROW LEVEL SECURITY;

-- "Google India Pvt. Ltd." → "google". Legal suffixes and "India" are dropped so spellings of one employer match.
CREATE OR REPLACE FUNCTION company_key(name TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(trim(regexp_replace(regexp_replace(regexp_replace(lower(name),
        '[.,&()''"]', ' ', 'g'),
        '\m(private|pvt|limited|ltd|inc|incorporated|llc|llp|corp|corporation|plc|gmbh|india|technologies|technology|solutions|services)\M', ' ', 'g'),
        '\s+', ' ', 'g')), '')
$$;
CREATE OR REPLACE FUNCTION linkedin_company_slug(url TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(substring(url FROM 'linkedin\.com/company/([^/?#]+)'))
$$;

-- Points jobs without a company at one: by LinkedIn company page first, then by name. Companies not seen
-- before are added (not premium). Returns how many jobs it linked.
CREATE OR REPLACE FUNCTION link_companies() RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    linked INTEGER := 0;
    n INTEGER;
    pass INTEGER;
BEGIN
    FOR pass IN 1..3 LOOP
        UPDATE jobs j SET company_slug = c.slug FROM companies c
        WHERE j.company_slug IS NULL AND c.linkedin_slug = linkedin_company_slug(j.company_url);
        GET DIAGNOSTICS n = ROW_COUNT; linked := linked + n;
        UPDATE jobs j SET company_slug = c.slug FROM companies c
        WHERE j.company_slug IS NULL AND company_key(j.company) = ANY (c.name_keys);
        GET DIAGNOSTICS n = ROW_COUNT; linked := linked + n;
        EXIT WHEN pass = 3;

        -- New companies, slugged from their LinkedIn page or name; the second pass adds a hash for slugs taken.
        INSERT INTO companies (slug, name, linkedin_slug, name_keys)
        SELECT DISTINCT ON (key)
               CASE WHEN pass = 1 AND base <> '' THEN base
                    ELSE left(base, 40) || CASE WHEN base = '' THEN 'c-' ELSE '-' END || left(md5(key), 6) END,
               company, ls, ARRAY[key]
        FROM (SELECT company, company_key(company) AS key, linkedin_company_slug(company_url) AS ls,
                     trim(BOTH '-' FROM regexp_replace(COALESCE(linkedin_company_slug(company_url), company_key(company)),
                                                       '[^a-z0-9]+', '-', 'g')) AS base,
                     first_seen_at
              FROM jobs WHERE company_slug IS NULL AND company_key(company) IS NOT NULL) t
        ORDER BY key, first_seen_at
        ON CONFLICT DO NOTHING;
    END LOOP;

    -- Learn each company's LinkedIn page (needed to look up its ID) and logo from its jobs.
    UPDATE companies c SET linkedin_slug = s.ls
    FROM (SELECT DISTINCT ON (ls) ls, company_slug
          FROM (SELECT DISTINCT company_slug, linkedin_company_slug(company_url) AS ls FROM jobs
                WHERE company_slug IN (SELECT slug FROM companies WHERE linkedin_slug IS NULL)) t
          WHERE ls IS NOT NULL ORDER BY ls, company_slug) s
    WHERE c.slug = s.company_slug AND c.linkedin_slug IS NULL
      AND NOT EXISTS (SELECT 1 FROM companies o WHERE o.linkedin_slug = s.ls);
    -- company_logo (schema.sql) is raw.companyLogo as a column: reading raw itself would load every job's full JSON
    -- from disk on every run. Jobs from the last day are enough, as the database only keeps 48 hours of them.
    UPDATE companies c SET logo_url = s.logo
    FROM (SELECT DISTINCT ON (company_slug) company_slug, company_logo AS logo FROM jobs
          WHERE company_slug IS NOT NULL AND company_logo LIKE 'https://%'
            AND first_seen_at > now() - interval '1 day'
          ORDER BY company_slug, first_seen_at DESC) s
    WHERE c.slug = s.company_slug AND c.logo_url IS DISTINCT FROM s.logo;
    RETURN linked;
END $$;

-- Moves jobs that match a company (by LinkedIn page or name) onto it, e.g. after it was added or got an alias,
-- and drops the auto-added companies left without jobs.
CREATE OR REPLACE FUNCTION relink_company(target TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE n INTEGER;
BEGIN
    UPDATE jobs j SET company_slug = c.slug FROM companies c
    WHERE c.slug = target AND j.company_slug IS DISTINCT FROM c.slug
      AND (c.linkedin_slug = linkedin_company_slug(j.company_url) OR company_key(j.company) = ANY (c.name_keys));
    GET DIAGNOSTICS n = ROW_COUNT;
    DELETE FROM companies c WHERE NOT c.is_premium AND c.linkedin_id IS NULL AND c.domain IS NULL
                              AND NOT EXISTS (SELECT 1 FROM jobs WHERE company_slug = c.slug);
    RETURN n;
END $$;

-- The website reads only these views, with Supabase's public anon key. The base tables have RLS on,
-- so that key can't read raw data, job-poster details or runs (descriptions only through
-- public_job, for the job pages). schema.sql makes the views run as the reader and lets that key read just the
-- columns and rows they show.
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
-- Only when it doesn't exist yet: schema.sql redefines it with more columns ("companyLogo", "places"), which a
-- CREATE OR REPLACE with these columns would try to drop.
DO $$ BEGIN
    IF to_regclass('public.public_jobs') IS NULL THEN
        CREATE VIEW public_jobs AS
            SELECT j.job_id AS id, j.title, j.company, j.location, j.category, j.workplace, j.url,
                   j.posted_at AS "postedAt", j.employment_type AS "employmentType", j.seniority,
                   j.first_seen_at AS "firstSeenAt", j.company_slug AS "companySlug",
                   COALESCE(c.is_premium, false) AS "companyPremium", j.role, j.level, j.tags
            FROM jobs j LEFT JOIN companies c ON c.slug = j.company_slug
            WHERE j.status <> 'skipped' AND j.first_seen_at > now() - interval '30 days';
    END IF;
END $$;
-- One job for its page on the site (web/job.html, linked from the board and LinkedIn posts): the listing with its
-- description, pay and company logo. No 30-day cut-off, so links in older posts keep working.
CREATE OR REPLACE VIEW public_job AS
    SELECT j.job_id AS id, j.title, j.company, j.location, j.category, j.workplace, j.url, j.apply_url AS "applyUrl",
           j.salary, j.description, j.posted_at AS "postedAt", j.employment_type AS "employmentType", j.seniority,
           j.first_seen_at AS "firstSeenAt", j.company_slug AS "companySlug",
           COALESCE(c.is_premium, false) AS "companyPremium", c.logo_url AS "companyLogo", c.domain AS "companyDomain",
           j.role, j.level, j.tags
    FROM jobs j LEFT JOIN companies c ON c.slug = j.company_slug
    WHERE j.status <> 'skipped';
CREATE OR REPLACE VIEW public_categories AS
    SELECT slug, label, sort_order FROM categories;
-- Tab counts, so the site doesn't have to download every job to count them.
CREATE OR REPLACE VIEW public_job_counts AS
    SELECT category, workplace, COUNT(*)::int AS n FROM public_jobs GROUP BY category, workplace;
-- Companies for the filter and the premium chips, with how many listed jobs each has.
CREATE OR REPLACE VIEW public_companies AS
    SELECT c.slug, c.name, c.domain, c.is_premium AS "isPremium", c.sort_order AS "sortOrder",
           COUNT(j.id)::int AS jobs,
           (COUNT(j.id) FILTER (WHERE j."firstSeenAt" > now() - interval '7 days'))::int AS "jobsWeek",
           c.logo_url AS "logoUrl", c.employees,
           sal.currency AS "salaryCurrency", sal.lo AS "salaryMin", sal.hi AS "salaryMax", sal.n AS "salaryRoles",
           c.glassdoor_rating AS "glassdoorRating"
    FROM companies c
    LEFT JOIN public_jobs j ON j."companySlug" = c.slug
    -- Lowest to highest median pay across its roles, in the currency most of its rows use.
    LEFT JOIN LATERAL (
        SELECT s.currency, MIN(s.median_annual) AS lo, MAX(s.median_annual) AS hi, COUNT(*)::int AS n
        FROM company_salaries s WHERE s.company_slug = c.slug
        GROUP BY s.currency ORDER BY COUNT(*) DESC, s.currency LIMIT 1) sal ON true
    GROUP BY c.slug, sal.currency, sal.lo, sal.hi, sal.n;
-- Per-role salaries for the site's dropdown, highest median first.
CREATE OR REPLACE VIEW public_company_salaries AS
    SELECT company_slug AS "companySlug", job_title AS title, currency,
           min_annual AS min, median_annual AS median, max_annual AS max,
           min_usd AS "minUsd", median_usd AS "medianUsd", max_usd AS "maxUsd", fetched_at AS "fetchedAt",
           query AS role   -- the role (salaries.roles) it was found by, for matching a job (web/pay-roles.js)
    FROM company_salaries;

-- Company index, 0 to 100, for ranking companies in LinkedIn posts and on the site:
--   Glassdoor rating            up to 30 (rating / 5; 12 when unknown, a middling rating)
--   premium (MNC) company       20
--   jobs first seen in 7 days   up to 25 (10 or more is full marks)
--   best median pay             up to 25 (its rank among companies with salaries, in USD)
CREATE OR REPLACE VIEW company_index AS
    SELECT c.slug, c.name, c.is_premium, c.glassdoor_rating, COALESCE(w.n, 0)::int AS jobs_week, pay.usd AS pay_usd,
           round((COALESCE(c.glassdoor_rating / 5 * 30, 12)
                 + CASE WHEN c.is_premium THEN 20 ELSE 0 END
                 + LEAST(COALESCE(w.n, 0), 10) * 2.5
                 + COALESCE(CASE WHEN pay.usd IS NOT NULL THEN percent_rank() OVER (PARTITION BY pay.usd IS NOT NULL ORDER BY pay.usd) END, 0) * 25)::numeric,
                 1) AS score
    FROM companies c
    LEFT JOIN LATERAL (SELECT COUNT(*) AS n FROM jobs j
                       WHERE j.company_slug = c.slug AND j.status <> 'skipped' AND j.first_seen_at > now() - interval '7 days') w ON true
    LEFT JOIN LATERAL (SELECT MAX(s.median_usd) AS usd FROM company_salaries s WHERE s.company_slug = c.slug) pay ON true;

-- LinkedIn "Hiring alert" posts, one per person (config linkedin.people) per scrape, written by
-- src/linkedin-posts.js, and the jobs in each. A job goes into one post only, ever. Each person's n8n reads its
-- post from public_linkedin_posts and reports back through web/api/post-status.mjs (see n8n/README.md).
CREATE TABLE IF NOT EXISTS posts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person       TEXT NOT NULL,                    -- linkedin.people[].id, e.g. animesh
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'posted', 'skipped')),
    body         TEXT,                             -- the post text
    linkedin_url TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS posts_person ON posts (person, created_at DESC);
ALTER TABLE posts ADD COLUMN IF NOT EXISTS image BYTEA;   -- the post's picture, a PNG (src/post-image.js)
ALTER TABLE posts ADD COLUMN IF NOT EXISTS first_comment TEXT;   -- the comment with the links, added under the post by hand
CREATE TABLE IF NOT EXISTS post_jobs (
    post_id  BIGINT NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    source   TEXT NOT NULL,
    job_id   TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,           -- order in the post
    PRIMARY KEY (post_id, source, job_id)
);
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_jobs ENABLE ROW LEVEL SECURITY;

-- Whether a job is for the LinkedIn posts' readers: an entry or mid level software role. The title must name a
-- software job, nothing senior (senior, lead, staff, manager, architect, VP, "Engineer 4" or "IV" and up; "Software
-- Engineer III" stays in), and not support, sales, consulting or other non-dev work; LinkedIn's seniority must not be
-- director, executive or internship.
CREATE OR REPLACE FUNCTION post_job_ok(title TEXT, seniority TEXT) RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(seniority, '') !~* '^(director|executive|internship)$'
       AND title ~* '(engineer|developer|\msde|programmer|software|scientist|data analyst|devops|\msre\M|full ?stack|front ?end|back ?end|android|\mios\M|mobile|machine learning|\mml\M|\mai\M|\mllm|cloud|security|\mqa\M|\mtest|automation)'
       AND title !~* '\m(senior|sr|lead|leader|principal|staff|manager|director|head|architect|vp|avp|vice president|chief|distinguished|fellow|advisory|intern|internship)\M'
       AND title !~* '(engineer|developer|scientist|analyst)\s*[-,]?\s*(iv|v|vi|[4-9])\M'
       AND title !~* '(support|customer|help ?desk|service desk|technical account|\msales\M|pre-?sales|recruit|\mbpo\M|voice process|call cent|desktop|operator|administrator|consultant|functional|business analyst|trainer|instructor|tutor|annotat|data entry|content|writer|contractor)'
$$;

-- Jobs worth a post: entry and mid level software jobs (post_job_ok) at tier 1 premium companies with a real
-- logo (premium.sql), first seen in the last 3 days, in no post yet, at most 2 per company, best first.
-- A job is left out when the same company's same title (ignoring case and punctuation) is newer in this list
-- or was already in a post in the last 30 days: LinkedIn often lists one opening several times, under new ids
-- or once per city.
-- Job score = company index (0 to 100) + up to 20 for freshness + 5 when it names its tech stack.
CREATE OR REPLACE VIEW post_candidates AS
    SELECT * FROM (
    SELECT source, job_id, title, company, company_slug, location, workplace, url, role, level, tags, first_seen_at,
           company_score, is_premium, score,
           row_number() OVER (PARTITION BY company_key ORDER BY first_seen_at DESC, job_id) AS nth_at_company,
           salary, apply_url
    FROM (
        SELECT j.source, j.job_id, j.title, j.company, j.company_slug, j.location, j.workplace, j.url,
               j.role, j.level, j.tags, j.first_seen_at, ci.score AS company_score, ci.is_premium,
               round((COALESCE(ci.score, 12)
                     + GREATEST(0, 20 - EXTRACT(EPOCH FROM now() - j.first_seen_at) / 3600 / 72 * 20)
                     + CASE WHEN cardinality(j.tags) > 0 THEN 5 ELSE 0 END)::numeric, 1) AS score,
               COALESCE(j.company_slug, j.company) AS company_key,
               row_number() OVER (PARTITION BY lower(COALESCE(j.company_slug, j.company)),
                                               regexp_replace(lower(j.title), '[^a-z0-9]+', '', 'g')
                                  ORDER BY j.first_seen_at DESC, j.job_id) AS nth_of_title,
               j.salary, j.apply_url
        FROM jobs j
        JOIN companies c ON c.slug = j.company_slug AND c.premium_tier = 1 AND c.logo_url LIKE '%media.licdn.com%'
        LEFT JOIN company_index ci ON ci.slug = j.company_slug
        WHERE j.status <> 'skipped' AND j.first_seen_at > now() - interval '3 days' AND post_job_ok(j.title, j.seniority)
          AND NOT EXISTS (SELECT 1 FROM post_jobs pj WHERE pj.source = j.source AND pj.job_id = j.job_id)
          AND NOT EXISTS (
              SELECT 1 FROM post_jobs pj
              JOIN posts p ON p.id = pj.post_id AND p.created_at > now() - interval '30 days'
              JOIN jobs pjj ON pjj.source = pj.source AND pjj.job_id = pj.job_id
              WHERE lower(COALESCE(pjj.company_slug, pjj.company)) = lower(COALESCE(j.company_slug, j.company))
                AND regexp_replace(lower(pjj.title), '[^a-z0-9]+', '', 'g') = regexp_replace(lower(j.title), '[^a-z0-9]+', '', 'g'))
    ) t
    WHERE nth_of_title = 1
    ) u
    WHERE nth_at_company <= 2
    ORDER BY score DESC, first_seen_at DESC;

-- Posts for the private posts page (web/posts.html) and for n8n. Job posts are public data, so the site's
-- public key may read them; the page's password only keeps the page out of casual view.
CREATE OR REPLACE VIEW public_linkedin_posts AS
    SELECT p.id, p.person, p.status, p.body, p.linkedin_url AS "linkedinUrl", p.created_at AS "createdAt",
           p.posted_at AS "postedAt", (SELECT COUNT(*)::int FROM post_jobs pj WHERE pj.post_id = p.id) AS jobs,
           p.first_comment AS "firstComment"
    FROM posts p;

-- Each post's picture as base64 PNG, for n8n (node "Picture to file" turns it back into an image).
CREATE OR REPLACE VIEW public_post_images AS
    SELECT id, replace(encode(image, 'base64'), E'\n', '') AS image FROM posts WHERE image IS NOT NULL;

-- The site's hiring-alert page (web/alert.html?id=<post id>): the jobs of one post, in post order.
CREATE OR REPLACE VIEW public_alert_jobs AS
    SELECT p.id AS "alertId", p.created_at AS "alertAt", pj.position, j.job_id AS id, j.title, j.company, j.location,
           j.workplace, j.url, j.company_slug AS "companySlug", COALESCE(c.is_premium, false) AS "companyPremium",
           j.role, j.level, j.tags, j.first_seen_at AS "firstSeenAt"
    FROM posts p
    JOIN post_jobs pj ON pj.post_id = p.id
    JOIN jobs j ON j.source = pj.source AND j.job_id = pj.job_id
    LEFT JOIN companies c ON c.slug = j.company_slug
    WHERE p.status <> 'skipped';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON jobs, categories, searches, companies, company_salaries, fx_rates FROM anon, authenticated;
        REVOKE ALL ON posts, post_jobs, company_index, post_candidates FROM anon, authenticated;
        REVOKE ALL ON public_jobs, public_job, public_categories, public_job_counts, public_companies, public_company_salaries,
                      public_alert_jobs, public_linkedin_posts, public_post_images FROM anon, authenticated;
        GRANT SELECT ON public_jobs, public_job, public_categories, public_job_counts, public_companies, public_company_salaries,
                        public_alert_jobs, public_linkedin_posts, public_post_images TO anon, authenticated;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS runs (
    id            BIGSERIAL PRIMARY KEY,
    search_name   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    apify_run_id  TEXT,
    status        TEXT NOT NULL,                 -- RUNNING | SUCCEEDED | FAILED
    items_found   INTEGER DEFAULT 0,
    items_new     INTEGER DEFAULT 0,
    items_skipped INTEGER DEFAULT 0,
    cost_usd      NUMERIC(10, 4),
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS apify_token TEXT;   -- label of the Apify token used

-- Apify accounts to spend, lowest priority first. When one has less than config minRemainingUsd
-- left this month, the scraper moves on to the next (src/tokens.js). The secret APIFY_TOKEN is
-- used last. RLS with no policies: the site's public key can't read these.
CREATE TABLE IF NOT EXISTS apify_tokens (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label             TEXT NOT NULL UNIQUE,          -- e.g. the Apify username
    token             TEXT NOT NULL UNIQUE,          -- apify_api_...
    is_active         BOOLEAN NOT NULL DEFAULT true,
    priority          INTEGER NOT NULL DEFAULT 100,  -- lower = used first
    monthly_limit_usd NUMERIC(10, 4),                -- filled in by the scraper
    used_usd          NUMERIC(10, 4),
    remaining_usd     NUMERIC(10, 4),
    cycle_end_at      TIMESTAMPTZ,                   -- when Apify resets this account's usage
    last_checked_at   TIMESTAMPTZ,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE apify_tokens ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON runs, apify_tokens FROM anon, authenticated;
    END IF;
END $$;

-- Telegram alerts: who wants which keywords (written by the bot webhook web/api/telegram.mjs), and which
-- jobs each subscriber already got (src/telegram-notify.js). Private: RLS with no policies.
CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id      BIGINT PRIMARY KEY,
    username     TEXT,
    keywords     JSONB NOT NULL DEFAULT '[]',     -- [{"text": "react.js", "anywhere": false}]
    locations    TEXT[] NOT NULL DEFAULT '{}',    -- e.g. {remote, bengaluru}; empty = anywhere
    is_active    BOOLEAN NOT NULL DEFAULT true,   -- false after /stop or when the user blocks the bot
    last_sent_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS telegram_sent (
    chat_id BIGINT NOT NULL REFERENCES telegram_subscribers (chat_id) ON DELETE CASCADE,
    source  TEXT NOT NULL,
    job_id  TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, source, job_id)
);
ALTER TABLE telegram_subscribers ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_sent ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON telegram_subscribers, telegram_sent FROM anon, authenticated;
    END IF;
END $$;
