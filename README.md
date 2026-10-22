# scrap-py

Krawlr job scraper in Python, run on GitHub Actions. It searches job platforms for every keyword × location in two
lists in Supabase, and saves the jobs to the `public.jobs` table the site reads. LinkedIn is the only platform so far:
it uses the LinkedIn scraper from [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`, pinned) and adds
the filters LinkedIn ignores when you're not logged in. Every job comes with its full description.

## Run it

**Repos:** changes are pushed to `animesh9893/scrap-py`; `proj-berry/scrap-py` (a fork of it) is the main repo, where
the schedule runs. After new commits land here, sync the fork (proj-berry/scrap-py → **Sync fork** → **Update branch**).

**On a schedule:** in `proj-berry/scrap-py` only, GitHub runs it every 10 minutes (`cron: '*/10 * * * *'`, UTC) in
plan mode: it runs the searches that are due from the [search lists](#search-lists), at most `MAX_TASKS` (4) per run,
each looking back to just before its previous run. Their artifacts are kept 7 days. GitHub starts scheduled runs on a best-effort
basis (often a few minutes late, occasionally skipped under load), only from `main`, and pauses them after 60 days
without repository activity. To pause it, disable the workflow under Actions → **Scrape jobs** → **⋯**.

**Back to back:** a planned run there that succeeds starts the next one at once (`gh workflow run`, mode `plan`,
`chain: true`), so the searches run one after another instead of waiting for the next 10-minute tick; the concurrency
group keeps one running and at most one waiting (a newer waiting run replaces the older, which shows as cancelled).
After a failure the chain stops until the schedule's next run. Repository variables (Settings → Secrets and variables
→ Actions → Variables): `CHAIN=false` turns it off; `MAX_TASKS` and `TIME_BUDGET_MINUTES` size each planned run
(default 4 searches, 8 minutes; keep the budget under the job's 60-minute timeout).

**On demand:** Actions → **Scrape jobs** → **Run workflow**, fill in the form (every field has a default). Mode
`single` runs the one search in the form; `plan` runs the due searches from the lists, like the schedule; `plan-only`
only prints what `plan` would search. It also runs on a push to `main` that changes the scraper, with the defaults and
without saving. To start it from n8n or a script:

```
POST https://api.github.com/repos/<owner>/scrap-py/actions/workflows/scrape.yml/dispatches
Authorization: Bearer <token with Actions: read and write on this repo>
Accept: application/vnd.github+json

{"ref": "main", "inputs": {"search_term": "data engineer", "location": "Pune", "experience": "entry level, associate"}}
```

**Full sweep:** Actions → **Full sweep** → **Run workflow** searches every keyword × location pair from the lists,
due or not, one after another with a minute between searches (`gap_seconds`), each looking back the full `hours_old`
(24), keeping up to `results` (50) jobs per pair, and saves them all. Jobs already saved are only marked seen again,
unless `refresh` is ticked. 63 pairs take about 2 to 3 hours (GitHub stops a job at 6). It shares a queue with the
schedule, so scheduled runs wait until it ends, then carry on from where it left them. Same as `MODE=sweep`
(`GAP_SECONDS`, default 60).

## Search lists

Two tables in Supabase say what to search; edit them in the table editor. `seed.sql` (run it once in the SQL editor,
after the first run has created the tables) adds frontend / backend / full stack / mobile keywords and seven cities.

| `search_keywords` | |
| --- | --- |
| `keyword`, `kind` | what's searched: a title or a skill (`frontend developer`, `java`) |
| `category` | the site's tab for the jobs it finds (`frontend`, `backend`, `fullstack`, `mobile`, `devops`, `data`, `ai`, `qa`, `security`) |
| `excluded_location_ids` | `locations.id`s it's never searched in |
| `excluded_sources` | platforms it's never searched on |
| `exclude_title_words`, `experience`, `hours_old`, `max_results` | its own filters, instead of the form's defaults |
| `interval_hours`, `priority`, `is_active` | how often it's due (default 2h), lower runs first, on / off |

| `locations` | |
| --- | --- |
| `name`, `country`, `aliases` | the place (`Bengaluru`, also `Bangalore`) |
| `excluded_sources` | platforms that aren't searched here (say, Naukri doesn't cover it) |
| `source_params` | each platform's own name for it: `{"linkedin": {"location": "Bengaluru, Karnataka, India"}}`; `term_suffix` is added to every keyword searched there (`{"linkedin": {"location": "United States", "term_suffix": "remote"}}` for "Remote - US") |
| `interval_hours`, `priority`, `is_active` | as for keywords; a pair uses the smaller interval and the sum of priorities |

`sources` lists the platforms (`linkedin`); `enabled = false` stops searching one. Each planned run takes every
active keyword × active location × enabled platform, drops the excluded pairs, keeps the ones not tried for their
interval, and runs the never-run ones first (by priority), then the most overdue weighted by priority (overdue ×
200 / priority), until `MAX_TASKS` or `TIME_BUDGET_MINUTES` (8). A pair looks back to an hour before its last
successful run (its first run: `hours_old`). The seed lists have 27 keywords × 22 locations, far more than 4 searches
every 10 minutes (about 576 a day) can keep at their intervals, so every pair still comes round but less often than
its interval says, the low-priority ones least; raise `MAX_TASKS`, lengthen intervals or deactivate rows to change
that.

Jobs already saved and seen in the last `KNOWN_DAYS` (7) are only marked as seen again: their job pages aren't
read, which saves most of the requests on a 10-minute schedule. A single search reads every page, so its report
shows every job (`SKIP_KNOWN` sets it either way).

## Saving to Supabase

With a `DATABASE_URL` repo secret (the Supabase Postgres connection string: Project settings → Database → Connection
string → URI, session pooler), every run saves its jobs to the database the old job-scraper built, so the site,
Telegram alerts and LinkedIn posts pick them up like any other job. `db.py` does it, each search in its own
transaction:

1. Applies `schema.sql`: only additions (new tables, columns and functions, below), safe to run every time.
2. For a single search, records it in the lists: the search term in `search_keywords`, the location in `locations`
   (added inactive the first time, so a one-off search doesn't join the schedule).
3. Upserts the jobs with `upsert_jobs()` on `(source, job_id)`. New jobs get the table defaults (`status` new,
   `first_seen_at`); a job seen before gets its scraped fields refreshed (kept when this scrape didn't get them) and
   `last_seen_at` bumped, keeps its `status`, tags, first category and `search_name`, and becomes `remote` if any
   search finds it remote. `workplace` is stored as the old pipeline did: `remote`, `hybrid` or null.
4. Records which search found each job in `job_search_hits` (also the known jobs it found again), and the run in
   `runs` (actor `scrap-py`, with the keyword, location and the run's meta).
5. After all the searches, links jobs to companies with the old `link_companies()` (adds companies seen for the first time).
6. Closes jobs not seen for `EXPIRE_DAYS` (default 7) with `expire_jobs()`: sets `jobs.closed_at`.

`category` (the site's tabs) comes from the `category` input, else the keyword's category in `search_keywords`, else
the title, as the old pipeline did. Runs started by a push, with **save** unticked or with `DRY_RUN=true` only write
the files.

| Added by `schema.sql` | For |
| --- | --- |
| `sources` | job platforms (`linkedin` now) |
| `search_keywords` | what to search: keyword, kind, category, `excluded_location_ids`, `excluded_sources`, per-keyword filters, priority |
| `locations` | where to search: name, aliases, `source_params` (each platform's name for it), `excluded_sources` |
| `job_search_hits` | which keyword × location found each job |
| `jobs.closed_at` | when the job stopped being seen |
| `runs.source`, `keyword_id`, `location_id`, `details` | what a scrap-py run searched, and its meta |
| `upsert_jobs(jsonb)`, `expire_jobs(days)` | the save and the expirer |
| `company_stats` | per company: open jobs, jobs this week / month, latest job |

Tagging, Telegram alerts and LinkedIn posts are still the old project's Node steps; they read the jobs from the
database, so they work on what this saves.

## Filters

LinkedIn's logged-out search applies only some filters itself. The scraper checks the others on each job, and keeps
paging until it has `results` jobs that pass, or until it has looked at `SCAN_LIMIT` jobs (default 5 × `results`).

| Input | Applied by | Notes |
| --- | --- | --- |
| `search_term`, `location` | LinkedIn | Keyword search, so some titles will be loose matches; use `exclude` |
| `hours_old` | LinkedIn, re-checked | Re-checked to the hour from "5 hours ago" |
| `company_ids`, `easy_apply` | LinkedIn | |
| `job_type` | scraper | From the job page's "Employment type" |
| `experience` | scraper | From the job page's "Seniority level", as the poster set it |
| `exclude` | scraper | Words in the title, e.g. `senior, lead, manager` |
| `sort` | scraper | LinkedIn ignores sorting; `recent` puts the jobs kept newest first |

**There is no remote filter.** LinkedIn ignores remote / hybrid / on-site for logged-out searches and doesn't show
the workplace type on the job page. Put "remote" in the search term instead (the "Remote - …" locations do that with
`term_suffix`).

**Tech jobs only.** Titles that name a non-tech job (sales, marketing, recruiting, HR, support, accounts, teaching,
mechanical or civil engineering…; `NON_TECH` in search.py) are skipped whatever the search, as `non_tech`.

Rejected jobs are logged with the reason. The counts per reason are in `jobs.json`, `jobs.md` and the report, which
also say when a run stopped at `SCAN_LIMIT` before finding `results` jobs. A value that isn't valid (an unknown
`job_type` or `experience`, a company name instead of an id, a `sort` other than recent / relevant) stops the run
with a message saying what's wrong, rather than silently filtering out every job.

## Output

Each run uploads an artifact **jobs-&lt;run number&gt;** (kept 30 days), downloadable from the run page. A single
search's files are at its top; a planned run puts each search's in its own folder (`01-linkedin-react-developer-in-pune/`)
and adds `summary.md`, a table of the searches with what each saved:

| File | For |
| --- | --- |
| `report.html` | Reading: open in a browser. One card per job, a search box, expandable descriptions, light / dark |
| `jobs.json` | Scripts / n8n / Supabase: `{"meta": {search, filters, counts, error}, "jobs": [rows]}` |
| `jobs.csv` | Excel / Sheets / `COPY`: the same rows (`raw` as a JSON string) |
| `jobs.md` | The run page's summary (and Slack / Notion / GitHub): a table of the jobs, then each description in a collapsible block |

### Rows (`public.jobs`)

Each job is a row of the `public.jobs` table, ready to upsert on `(source, job_id)`:

| Column | From LinkedIn |
| --- | --- |
| `source`, `job_id` | `linkedin` and the job's id |
| `title`, `company`, `company_url`, `location`, `url` | the job |
| `company_slug` | from `company_url` (`/company/google` → `google`) |
| `workplace` | Remote / Hybrid / On-site, only when the posting states it (title, location, "Work mode: Hybrid", "(Onsite)", "hybrid working environment"); LinkedIn hides its own workplace field from logged-out visitors, so it's often null |
| `apply_url` | the job's LinkedIn URL for Easy Apply jobs; null for jobs that apply on the company's site (LinkedIn only shows that link after sign-in) |
| `posted_at` | UTC time to the minute / hour from "51 minutes ago" / "5 hours ago"; just the date for jobs a day or older |
| `employment_type`, `seniority` | the job page's "Employment type" and "Seniority level" |
| `salary` | LinkedIn's pay range, e.g. `₹1,400,000.00/yr - ₹2,800,000.00/yr`, when the poster gave one |
| `description` | the full description, Markdown |
| `search_name` | `SEARCH_NAME` (default "&lt;search term&gt; in &lt;location&gt;") |
| `last_seen_at` | when the job was scraped |
| `raw` | everything else: `applicants` / `applicants_text`, `apply_type` (easy_apply / company_site), `salary_min` / `max` / `currency` / `interval`, `job_function`, `industries`, `experience_years` ({min, max}), `sections` (the description cut into `summary`, `responsibilities`, `requirements`, `qualifications`, `benefits`, `about_company` at its headings; best effort), `posted_ago`, `posted_date`, `emails`, `company_logo`, `description_html` (cleaned, safe to embed), `page_read` (false when the job page failed and only the search card's fields are there), and the search |

`status`, `first_seen_at` (table defaults) and `category`, `role`, `level`, `tags`, `tagged_at` (for a tagging step)
are left out of the rows, so an upsert keeps their values.

## Settings

Workflow inputs map to environment variables read by `scrape.py` (`SEARCH_TERM`, `LOCATION`, `HOURS_OLD`,
`RESULTS`, `JOB_TYPE`, `EXPERIENCE`, `EXCLUDE`, `COMPANY_IDS`, `EASY_APPLY`, `SORT`, `CATEGORY`, `MODE`, `DRY_RUN`); in
plan mode they're the defaults the lists' own settings override. `DATABASE_URL`, `EXPIRE_DAYS`, `KNOWN_DAYS`,
`SKIP_KNOWN`, `MAX_TASKS`, `DEFAULT_INTERVAL_HOURS`, `TIME_BUDGET_MINUTES`, `SEARCH_NAME`, `DISTANCE` (miles, default
50), `SCAN_LIMIT` and `OUT_DIR` can be set as environment variables only. If LinkedIn starts blocking GitHub's runners
(empty results, 429s), add a repo secret `PROXIES` with comma-separated `user:pass@host:port` proxies. Requests
LinkedIn rate-limits (429) or fails (5xx) are retried twice, after 5 and 15 seconds, before a page is given up on.

No LinkedIn login is needed or supported: the scraper reads LinkedIn's public pages. Scraping while signed in breaks
LinkedIn's User Agreement and gets accounts restricted.

## Locally

```
pip install -r requirements.txt
cp .env.example .env              # optional: settings for local runs (gitignored)
SEARCH_TERM="data engineer" EXPERIENCE="entry level" python scrape.py     # writes out/, open out/report.html
MODE=plan-only DATABASE_URL=... python scrape.py                           # what the schedule would search next
MODE=plan-only PLAN_ALL=true DATABASE_URL=... python scrape.py             # what a full sweep would search
pytest tests
```

`scrape.py` reads `.env` on start; variables set in the environment (as above) win over it.

## Code

| File | |
| --- | --- |
| `scrape.py` | the run: mode, searches, outputs, saving |
| `search.py` | one search's settings (`Search`), from the environment or the lists |
| `planner.py` | which searches are due |
| `sources/` | one module per platform, all with the same interface (`sources/__init__.py`); `linkedin.py` now |
| `report.py` | `jobs.json`, `jobs.csv`, `jobs.md`, `report.html`, `summary.md` |
| `db.py`, `schema.sql`, `seed.sql` | Supabase |
| `tests/` | `pytest tests`: LinkedIn against saved pages, the planner; `TEST_DATABASE_URL` (a throwaway Postgres with the old project's schema) adds the database tests |

A new platform (Naukri, …) is a new `sources/<name>.py` with `NAME`, `LABEL`, `problems(search)` and
`run(search, known)` returning rows shaped like `public.jobs`, added to `sources.AVAILABLE` and the `sources` table.

## Website

`web/` is the job board at https://krawlr.xyz, moved here unchanged from the old job-scraper repo: plain HTML and JS, no build step. Every page reads the Supabase `public_*` views in the browser with the publishable key in `web/config.js`; that key can only read those views. `netlify/functions/` holds the Netlify functions for the Telegram bot (`/api/telegram`) and LinkedIn post status (`/api/post-status`); they need `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` and `POST_TOKEN_<PERSON>` in Netlify's environment variables. `netlify.toml` publishes `web/` with no build step.

`posts/` writes the LinkedIn hiring-alert posts and their pictures every hour (Node, `.github/workflows/posts.yml`); each person's n8n asks for approval on Telegram and posts them. See `posts/README.md`.
