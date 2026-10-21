# scrap-py

Krawlr LinkedIn job scraper in Python, run on GitHub Actions. It uses the LinkedIn scraper from
[JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`, pinned) and adds the filters LinkedIn ignores when
you're not logged in. Every job comes with its full description.

## Run it

Actions → **Scrape jobs** → **Run workflow**, fill in the form (every field has a default). It also runs on a push
to `main` that changes the scraper, with the defaults. There is no schedule; to start it from n8n or a script:

```
POST https://api.github.com/repos/<owner>/scrap-py/actions/workflows/scrape.yml/dispatches
Authorization: Bearer <token with Actions: read and write on this repo>
Accept: application/vnd.github+json

{"ref": "main", "inputs": {"search_term": "data engineer", "location": "Pune", "experience": "entry level, associate"}}
```

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
the workplace type on the job page. Put "remote" in the search term instead.

Rejected jobs are logged with the reason. The counts per reason are in `jobs.json`, `jobs.md` and the report, which
also say when a run stopped at `SCAN_LIMIT` before finding `results` jobs. A value that isn't valid (an unknown
`job_type` or `experience`, a company name instead of an id, a `sort` other than recent / relevant) stops the run
with a message saying what's wrong, rather than silently filtering out every job.

## Output

Each run uploads an artifact **jobs-&lt;run number&gt;** (kept 30 days), downloadable from the run page:

| File | For |
| --- | --- |
| `report.html` | Reading: open in a browser. One card per job, a search box, expandable descriptions, light / dark |
| `jobs.json` | Scripts / n8n / Supabase: `{"meta": {search, filters, counts, error}, "jobs": [rows]}` |
| `jobs.csv` | Excel / Sheets / `COPY`: the same rows (`raw` as a JSON string) |
| `jobs.md` | Pasting into Slack / Notion / GitHub: a table of the jobs. Also shown on the run page |

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
`RESULTS`, `JOB_TYPE`, `EXPERIENCE`, `EXCLUDE`, `COMPANY_IDS`, `EASY_APPLY`, `SORT`). `SEARCH_NAME`, `DISTANCE` (miles,
default 50), `SCAN_LIMIT` and `OUT_DIR` can be set as environment variables only. If LinkedIn starts blocking GitHub's runners
(empty results, 429s), add a repo secret `PROXIES` with comma-separated `user:pass@host:port` proxies.

## Locally

```
pip install -r requirements.txt
SEARCH_TERM="data engineer" EXPERIENCE="entry level" python scrape.py     # writes out/, open out/report.html
```
