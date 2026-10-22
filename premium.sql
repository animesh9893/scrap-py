-- The premium companies: well-known names, picked by hand from the companies the scraper has found (2026-10-22).
-- Tier 1 companies are the only ones LinkedIn posts feature (post_candidates in base.sql); tier 2 (big IT services
-- and Big 4) are premium on the site and in the posts' logo strip, but never in a post. hashtag is the brand's own
-- tag, used in posts. A company is only featured while it has a real LinkedIn logo.
--
-- To add one: find its slug (SELECT slug, name FROM companies WHERE name ILIKE '%flipkart%') and add a line. To drop
-- one, delete its line: the next run clears it. Applied with base.sql and schema.sql when any of them changes (db.py).
-- Only companies that already exist are set; a slug the scraper hasn't found yet is picked up the next time this
-- file changes.
WITH list (slug, tier, hashtag) AS (VALUES
    -- Big tech
    ('google', 1, 'google'), ('microsoft', 1, 'microsoft'), ('amazon', 1, 'amazon'), ('amazon-web-services', 1, 'aws'),
    ('meta', 1, 'meta'), ('apple', 1, 'apple'), ('netflix', 1, 'netflix'), ('nvidia', 1, 'nvidia'), ('openai', 1, 'openai'),
    ('anthropicresearch', 1, 'anthropic'), ('linkedin', 1, 'linkedin'), ('github', 1, 'github'),
    ('tesla-motors', 1, 'tesla'), ('spacex', 1, 'spacex'),
    -- Software
    ('adobe', 1, 'adobe'), ('salesforce', 1, 'salesforce'), ('oracle', 1, 'oracle'), ('ibm', 1, 'ibm'), ('sap', 1, 'sap'),
    ('servicenow', 1, 'servicenow'), ('atlassian', 1, 'atlassian'), ('intuit', 1, 'intuit'), ('autodesk', 1, 'autodesk'),
    ('workday', 1, 'workday'), ('docusign', 1, 'docusign'), ('mongodbinc', 1, 'mongodb'),
    ('snowflake-computing', 1, 'snowflake'), ('databricks', 1, 'databricks'), ('cloudflare', 1, 'cloudflare'),
    ('datadog', 1, 'datadog'), ('crowdstrike', 1, 'crowdstrike'), ('palo-alto-networks', 1, 'paloaltonetworks'),
    ('okta-inc', 1, 'okta'), ('nutanix', 1, 'nutanix'), ('elastic-co', 1, 'elastic'), ('rubrik-inc', 1, 'rubrik'),
    ('cohesity', 1, 'cohesity'), ('zendesk', 1, 'zendesk'), ('postman-platform', 1, 'postman'),
    ('browserstack', 1, 'browserstack'), ('electronic-arts', 1, 'ea'), ('sony-interactive-entertainment-llc', 1, 'playstation'),
    -- Chips and hardware
    ('intel-corporation', 1, 'intel'), ('amd', 1, 'amd'), ('qualcomm', 1, 'qualcomm'), ('cisco', 1, 'cisco'),
    ('broadcom', 1, 'broadcom'), ('arm', 1, 'arm'), ('synopsys', 1, 'synopsys'), ('cadence', 1, 'cadence'),
    ('micron-technology', 1, 'micron'), ('applied-materials-india', 1, 'appliedmaterials'), ('klacorp', 1, 'kla'),
    ('texas-instruments-india', 1, 'texasinstruments'), ('samsungsemiconductor', 1, 'samsung'),
    ('delltechnologies', 1, 'dell'), ('hewlett-packard-enterprise', 1, 'hpe'),
    -- Consumer internet and retail tech
    ('uber-com', 1, 'uber'), ('paypal', 1, 'paypal'), ('stripe', 1, 'stripe'), ('tiktok', 1, 'tiktok'),
    ('snap-inc-co', 1, 'snapchat'), ('pinterest', 1, 'pinterest'), ('reddit-com', 1, 'reddit'), ('ebay', 1, 'ebay'),
    ('expediagroup', 1, 'expedia'), ('bookingholdings', 1, 'booking'), ('agoda', 1, 'agoda'), ('doordash', 1, 'doordash'),
    ('lyft', 1, 'lyft'), ('coinbase', 1, 'coinbase'), ('walmartglobaltechindia', 1, 'walmart'), ('walmart', 1, 'walmart'),
    ('target', 1, 'target'),
    -- India product companies
    ('phonepe-internet', 1, 'phonepe'), ('razorpay', 1, 'razorpay'), ('swiggy-in', 1, 'swiggy'), ('meesho', 1, 'meesho'),
    ('makemytrip-com', 1, 'makemytrip'), ('chargebee', 1, 'chargebee'), ('druva', 1, 'druva'),
    -- Banks and payments
    ('jpmorganchase', 1, 'jpmorgan'), ('goldman-sachs', 1, 'goldmansachs'), ('morgan-stanley', 1, 'morganstanley'),
    ('citi', 1, 'citi'), ('mastercard', 1, 'mastercard'), ('visa', 1, 'visa'), ('american-express', 1, 'americanexpress'),
    ('capital-one', 1, 'capitalone'), ('bank-of-america', 1, 'bankofamerica'), ('barclays-bank', 1, 'barclays'),
    ('hsbc', 1, 'hsbc'), ('deutsche-bank', 1, 'deutschebank'), ('ubs', 1, 'ubs'), ('wellsfargo', 1, 'wellsfargo'),
    -- Tier 2: IT services and Big 4
    ('accentureindia', 2, 'accenture'), ('accenture', 2, 'accenture'), ('deloitte', 2, 'deloitte'), ('ernstandyoung', 2, 'ey'),
    ('pwc-india', 2, 'pwc'), ('kpmg-us', 2, 'kpmg'), ('tata-consultancy-services', 2, 'tcs'), ('infosys', 2, 'infosys'),
    ('wipro', 2, 'wipro'), ('hcltech', 2, 'hcltech'), ('cognizant', 2, 'cognizant'), ('capgemini', 2, 'capgemini'),
    ('thoughtworks', 2, 'thoughtworks')
), dropped AS (
    UPDATE companies c SET premium_tier = NULL, hashtag = NULL, is_premium = false, premium_source = NULL
    WHERE c.premium_tier IS NOT NULL AND c.slug NOT IN (SELECT slug FROM list)
)
UPDATE companies c SET premium_tier = l.tier, hashtag = l.hashtag, is_premium = true, premium_source = 'default'
FROM list l
WHERE c.slug = l.slug
  AND (c.premium_tier, c.hashtag, c.is_premium) IS DISTINCT FROM (l.tier::SMALLINT, l.hashtag, true);
