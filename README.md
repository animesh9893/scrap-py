# scrap-py

Krawlr job scraper in Python, run on GitHub Actions:

- **Job boards** via [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`): Naukri, Indeed, LinkedIn
  (also Glassdoor, Google, ZipRecruiter).
- **ATS public APIs**: Greenhouse, Lever, Ashby (`ats:slug`, or a bare slug to auto-detect).

## Run it

Actions → **Scrape jobs** → **Run workflow**, fill in the form (every field has a default). It also runs on a push
to `main` that changes the scraper, with the defaults. There is no schedule; to start it from n8n or a script:

```
POST https://api.github.com/repos/<owner>/scrap-py/actions/workflows/scrape.yml/dispatches
Authorization: Bearer <token with Actions: read and write on this repo>
Accept: application/vnd.github+json

{"ref": "main", "inputs": {"search_term": "data engineer", "location": "Pune"}}
```

## Output

Each run uploads an artifact **jobs-&lt;run number&gt;** (kept 30 days), downloadable from the run page:

| File | What |
| --- | --- |
| `boards_<stamp>.csv` / `.json` | Every column JobSpy returns (description, salary, company info, …) |
| `ats_<stamp>.csv` / `.json` | Every ATS posting, with `description_html` and department |
| `all_jobs_<stamp>.csv` / `.json` | Common columns (source, company, title, location, url, posted_at), de-duplicated by url |
| `summary.json` | Jobs per site / company, errors, and the settings used |

The run page also shows a table of jobs per source.

## Settings

Workflow inputs map to environment variables read by `scrape.py` (`SEARCH_TERM`, `LOCATION`, `COUNTRY`, `SITES`,
`RESULTS`, `HOURS_OLD`, `COMPANIES`, `RUN_BOARDS`, `RUN_ATS`, `FETCH_DESC`, `OUT_DIR`). If a board starts
blocking GitHub's runners (empty results, 429s), add a repo secret `PROXIES` with comma-separated
`user:pass@host:port` proxies.

## Locally

```
pip install -U -r requirements.txt
SEARCH_TERM="data engineer" python scrape.py     # writes out/
```
