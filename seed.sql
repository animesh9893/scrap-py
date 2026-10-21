-- Starting search lists. Run once in Supabase's SQL editor after the first scrap-py run (which creates the tables
-- from base.sql and schema.sql), then edit the rows there: is_active, priority, interval_hours, the exclusions. Rows that already
-- exist (same keyword / location, any case) are left as they are.

-- The site's tabs (search_keywords.category points here): the old project's four, then the ones added since.
-- Tech jobs only: the scraper also skips non-tech titles (search.NON_TECH).
INSERT INTO categories (slug, label, sort_order) VALUES
    ('frontend', 'Frontend', 10), ('backend', 'Backend', 20), ('fullstack', 'Full stack', 30), ('mobile', 'Mobile', 40),
    ('devops', 'DevOps & Cloud', 50), ('data', 'Data', 60), ('ai', 'AI / ML', 70), ('qa', 'QA & Testing', 80),
    ('security', 'Security', 90)
ON CONFLICT DO NOTHING;

INSERT INTO search_keywords (keyword, kind, category, priority) VALUES
    ('frontend developer',   'title', 'frontend',  100),
    ('react developer',      'title', 'frontend',  100),
    ('backend developer',    'title', 'backend',   100),
    ('java developer',       'title', 'backend',   100),
    ('node.js developer',    'title', 'backend',   110),
    ('python developer',     'title', 'backend',   110),
    ('full stack developer', 'title', 'fullstack', 100),
    ('android developer',    'title', 'mobile',    120),
    ('ios developer',        'title', 'mobile',    120),
    ('angular developer',         'title', 'frontend',  110),
    ('golang developer',          'title', 'backend',   110),
    ('.net developer',            'title', 'backend',   110),
    ('mern stack developer',      'title', 'fullstack', 120),
    ('flutter developer',         'title', 'mobile',    120),
    ('react native developer',    'title', 'mobile',    120),
    ('devops engineer',           'title', 'devops',    100),
    ('site reliability engineer', 'title', 'devops',    110),
    ('cloud engineer',            'title', 'devops',    110),
    ('platform engineer',         'title', 'devops',    120),
    ('data engineer',             'title', 'data',      100),
    ('data scientist',            'title', 'data',      110),
    ('data analyst',              'title', 'data',      120),
    ('machine learning engineer', 'title', 'ai',        100),
    ('ai engineer',               'title', 'ai',        100),
    ('qa automation engineer',    'title', 'qa',        110),
    ('sdet',                      'title', 'qa',        120),
    ('security engineer',         'title', 'security',  120)
ON CONFLICT DO NOTHING;

INSERT INTO locations (name, country, aliases, priority) VALUES
    ('Bengaluru', 'IN', '{Bangalore}', 100),
    ('Pune',      'IN', '{}',          110),
    ('Hyderabad', 'IN', '{}',          110),
    ('Gurugram',  'IN', '{Gurgaon}',   120),
    ('Noida',     'IN', '{}',          120),
    ('Mumbai',    'IN', '{Bombay}',    120),
    ('Chennai',   'IN', '{Madras}',    120),
    ('Delhi',     'IN', '{New Delhi}', 130),
    ('Kolkata',   'IN', '{Calcutta}',  140),
    ('Ahmedabad', 'IN', '{}',          140)
ON CONFLICT DO NOTHING;

-- Outside India: searched less often (interval_hours) so the Indian cities keep their turns.
INSERT INTO locations (name, country, aliases, priority, interval_hours, source_params) VALUES
    ('United States',          'US', '{USA}',            140, 12, '{}'),
    ('San Francisco Bay Area', 'US', '{San Francisco}',  140, 12, '{}'),
    ('New York',               'US', '{NYC}',            140, 12, '{"linkedin": {"location": "New York, United States"}}'),
    ('Seattle',                'US', '{}',               150, 12, '{"linkedin": {"location": "Seattle, Washington, United States"}}'),
    ('Austin',                 'US', '{}',               150, 12, '{"linkedin": {"location": "Austin, Texas, United States"}}'),
    ('Canada',                 'CA', '{}',               140, 12, '{}'),
    ('Toronto',                'CA', '{}',               140, 12, '{"linkedin": {"location": "Toronto, Ontario, Canada"}}'),
    ('Vancouver',              'CA', '{}',               150, 12, '{"linkedin": {"location": "Vancouver, British Columbia, Canada"}}')
ON CONFLICT DO NOTHING;

-- Remote: LinkedIn's logged-out search ignores its remote filter, so these search the country with "remote" added
-- to the keyword (planner.py's term_suffix).
INSERT INTO locations (name, country, aliases, priority, interval_hours, source_params) VALUES
    ('Remote - India',     'IN', '{}', 120, 6,  '{"linkedin": {"location": "India", "term_suffix": "remote"}}'),
    ('Remote - US',        'US', '{}', 130, 12, '{"linkedin": {"location": "United States", "term_suffix": "remote"}}'),
    ('Remote - Canada',    'CA', '{}', 140, 12, '{"linkedin": {"location": "Canada", "term_suffix": "remote"}}'),
    ('Remote - Worldwide', NULL, '{}', 140, 12, '{"linkedin": {"location": "Worldwide", "term_suffix": "remote"}}')
ON CONFLICT DO NOTHING;

-- Examples (uncomment to use):
-- A keyword never searched in some locations:
--   UPDATE search_keywords SET excluded_location_ids = ARRAY(SELECT id FROM locations WHERE name IN ('Noida'))
--   WHERE keyword = 'ios developer';
-- A platform not searched in a location:
--   UPDATE locations SET excluded_sources = '{naukri}' WHERE name = 'Gurugram';
-- A platform's own name for a place:
--   UPDATE locations SET source_params = '{"linkedin": {"location": "Bengaluru, Karnataka, India"}}' WHERE name = 'Bengaluru';
