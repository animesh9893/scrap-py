-- Starting search lists. Run once in Supabase's SQL editor after the first scrap-py run (which creates the tables
-- from base.sql and schema.sql), then edit the rows there: is_active, priority, interval_hours, the exclusions. Rows that already
-- exist (same keyword / location, any case) are left as they are.

-- The site's tabs (search_keywords.category points here), as the old project seeded them.
INSERT INTO categories (slug, label, sort_order) VALUES
    ('frontend', 'Frontend', 10), ('backend', 'Backend', 20), ('fullstack', 'Full stack', 30), ('mobile', 'Mobile', 40)
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
    ('ios developer',        'title', 'mobile',    120)
ON CONFLICT DO NOTHING;

INSERT INTO locations (name, country, aliases, priority) VALUES
    ('Bengaluru', 'IN', '{Bangalore}', 100),
    ('Pune',      'IN', '{}',          110),
    ('Hyderabad', 'IN', '{}',          110),
    ('Gurugram',  'IN', '{Gurgaon}',   120),
    ('Noida',     'IN', '{}',          120),
    ('Mumbai',    'IN', '{Bombay}',    120),
    ('Chennai',   'IN', '{Madras}',    120)
ON CONFLICT DO NOTHING;

-- Examples (uncomment to use):
-- A keyword never searched in some locations:
--   UPDATE search_keywords SET excluded_location_ids = ARRAY(SELECT id FROM locations WHERE name IN ('Noida'))
--   WHERE keyword = 'ios developer';
-- A platform not searched in a location:
--   UPDATE locations SET excluded_sources = '{naukri}' WHERE name = 'Gurugram';
-- A platform's own name for a place:
--   UPDATE locations SET source_params = '{"linkedin": {"location": "Bengaluru, Karnataka, India"}}' WHERE name = 'Bengaluru';
