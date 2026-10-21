"""Krawlr job scraper: JobSpy (Naukri / LinkedIn / Indeed) + ATS (Greenhouse / Lever / Ashby).

Runs on GitHub Actions (.github/workflows/scrape.yml) and writes everything to OUT_DIR (default out/), which the
workflow uploads as the run's artifact:
  boards_<stamp>.csv / .json   every column JobSpy returns, one row per job board posting
  ats_<stamp>.csv / .json      every ATS posting, with description_html
  all_jobs_<stamp>.csv / .json common columns of both, de-duplicated by url
  summary.json                 counts per source and per site/company, plus any errors

Every setting can be overridden with an environment variable of the same name (the workflow's inputs set them).
Lists are comma-separated.
"""

import inspect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import requests
from jobspy import scrape_jobs


def env(name, default):
    value = (os.environ.get(name) or "").strip()
    return value if value else default


def env_list(name, default):
    value = env(name, None)
    return [x.strip() for x in value.split(",") if x.strip()] if value else default


def env_bool(name, default):
    value = env(name, None)
    return value.lower() in ("1", "true", "yes", "y") if value else default


# ---------------------------- CONFIG ----------------------------
RUN_BOARDS   = env_bool("RUN_BOARDS", True)
RUN_ATS      = env_bool("RUN_ATS", True)

SITES        = env_list("SITES", ["naukri", "indeed", "linkedin", "glassdoor"])   # also: google, zip_recruiter
SEARCH_TERM  = env("SEARCH_TERM", "software engineer")
LOCATION     = env("LOCATION", "Bengaluru")
COUNTRY      = env("COUNTRY", "India")          # for Indeed / Glassdoor
RESULTS      = int(env("RESULTS", "30"))        # per site
HOURS_OLD    = int(env("HOURS_OLD", "24"))
FETCH_DESC   = env_bool("FETCH_DESC", True)     # full descriptions: one extra request per LinkedIn / Naukri / Glassdoor job
PROXIES      = env_list("PROXIES", None)        # e.g. user:pass@host:port,host2:port (a repo secret)

# "ats:slug" or bare "slug" (auto-detect greenhouse -> lever -> ashby)
COMPANIES    = env_list("COMPANIES", ["greenhouse:gitlab", "lever:spotify", "ashby:linear", "stripe"])

OUT_DIR      = env("OUT_DIR", "out")
# ----------------------------------------------------------------

summary = {"boards": {}, "ats": {}, "errors": []}


# ============ 1. Job boards via JobSpy ============
def run_boards():
    params = set(inspect.signature(scrape_jobs).parameters)
    kw = dict(site_name=SITES, search_term=SEARCH_TERM, location=LOCATION,
              results_wanted=RESULTS, hours_old=HOURS_OLD, country_indeed=COUNTRY,
              proxies=PROXIES, verbose=1)
    # JobSpy renamed this param across versions
    if "fetch_description" in params:            kw["fetch_description"] = FETCH_DESC
    elif "linkedin_fetch_description" in params: kw["linkedin_fetch_description"] = FETCH_DESC
    kw = {k: v for k, v in kw.items() if k in params}

    df = pd.DataFrame()
    for site in SITES:                     # one site at a time so one failure doesn't kill all
        try:
            t = time.time()
            part = scrape_jobs(**{**kw, "site_name": [site]})
            print(f"✅ {site:<12} {len(part):>4} jobs  ({time.time()-t:.1f}s)")
            summary["boards"][site] = len(part)
            df = pd.concat([df, part], ignore_index=True)
        except Exception as e:
            print(f"❌ {site:<12} failed: {str(e)[:150]}")
            summary["boards"][site] = 0
            summary["errors"].append(f"{site}: {str(e)[:300]}")
    return df


# ============ 2. ATS public APIs ============
S = requests.Session()
S.headers["User-Agent"] = "krawlr-test/0.1"

def greenhouse(slug):
    r = S.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}, timeout=20)
    r.raise_for_status()
    return [{"source": "greenhouse", "company": slug, "id": str(j["id"]), "title": j.get("title"),
             "location": (j.get("location") or {}).get("name"),
             "department": ", ".join(d["name"] for d in j.get("departments", [])) or None,
             "url": j.get("absolute_url"), "posted_at": j.get("first_published") or j.get("updated_at"),
             "description_html": j.get("content")} for j in r.json().get("jobs", [])]

def lever(slug):
    r = S.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json():
        c, ts = j.get("categories") or {}, j.get("createdAt")
        out.append({"source": "lever", "company": slug, "id": j.get("id"), "title": j.get("text"),
                    "location": c.get("location"), "department": c.get("team"), "url": j.get("hostedUrl"),
                    "posted_at": datetime.fromtimestamp(ts/1000, timezone.utc).isoformat() if ts else None,
                    "description_html": j.get("description")})
    return out

def ashby(slug):
    r = S.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}, timeout=20)
    r.raise_for_status()
    return [{"source": "ashby", "company": slug, "id": j.get("id"), "title": j.get("title"),
             "location": j.get("location"), "department": j.get("department"), "url": j.get("jobUrl"),
             "posted_at": j.get("publishedAt"), "description_html": j.get("descriptionHtml")}
            for j in r.json().get("jobs", [])]

FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}

def fetch_company(spec):
    if ":" in spec:
        ats, slug = spec.split(":", 1)
        try:    return spec, FETCHERS[ats](slug)
        except Exception as e: return spec, f"error: {str(e)[:100]}"
    for ats, fn in FETCHERS.items():
        try:
            jobs = fn(spec)
            if jobs: return f"{ats}:{spec}", jobs
        except Exception:
            continue
    return spec, "not found on greenhouse/lever/ashby"

def run_ats():
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_company, COMPANIES))
    rows = []
    for spec, res in results:
        if isinstance(res, str):
            print(f"❌ {spec:<22} {res}")
            summary["ats"][spec] = 0
            summary["errors"].append(f"{spec}: {res}")
        else:
            print(f"✅ {spec:<22} {len(res):>4} jobs")
            summary["ats"][spec] = len(res)
            rows += res
    return pd.DataFrame(rows)


# ============ 3. Run, combine, save ============
def save(df, name):
    """CSV and JSON (JSON keeps descriptions and types intact; CSV opens in Excel / Sheets)."""
    df.to_csv(os.path.join(OUT_DIR, f"{name}.csv"), index=False)
    df.to_json(os.path.join(OUT_DIR, f"{name}.json"), orient="records", date_format="iso", indent=1,
               force_ascii=False)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    boards_df, ats_df = pd.DataFrame(), pd.DataFrame()

    if RUN_BOARDS:
        print(f"\n=== JOB BOARDS: {SEARCH_TERM!r} in {LOCATION!r}, last {HOURS_OLD}h, {RESULTS}/site ===")
        boards_df = run_boards()
        if len(boards_df): save(boards_df, f"boards_{stamp}")

    if RUN_ATS:
        print("\n=== ATS ===")
        ats_df = run_ats()
        if len(ats_df): save(ats_df, f"ats_{stamp}")

    # Normalized combined view (common columns)
    parts = []
    if len(boards_df):
        b = boards_df.rename(columns={"site": "source", "job_url": "url", "date_posted": "posted_at"})
        parts.append(b.reindex(columns=["source", "company", "title", "location", "url", "posted_at"]))
    if len(ats_df):
        parts.append(ats_df.reindex(columns=["source", "company", "title", "location", "url", "posted_at"]))

    all_df = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["url"]) if parts else pd.DataFrame()
    print(f"\n🎯 TOTAL: {len(all_df)} unique jobs")
    if len(all_df):
        print(all_df["source"].value_counts().to_string())
        save(all_df, f"all_jobs_{stamp}")
        print(all_df.head(25).to_string(max_colwidth=50))

    summary.update(stamp=stamp, total_unique=len(all_df),
                   by_source=all_df["source"].value_counts().to_dict() if len(all_df) else {},
                   config=dict(sites=SITES, search_term=SEARCH_TERM, location=LOCATION, country=COUNTRY,
                               results=RESULTS, hours_old=HOURS_OLD, fetch_desc=FETCH_DESC,
                               companies=COMPANIES, proxies=bool(PROXIES)))
    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)

    # Markdown summary on the run page
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        lines = [f"### {len(all_df)} unique jobs", "", "| Source | Jobs |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in {**summary["boards"], **summary["ats"]}.items()]
        if summary["errors"]:
            lines += ["", "**Errors**", ""] + [f"- {e}" for e in summary["errors"]]
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(lines) + "\n")

    if not len(all_df):
        raise SystemExit("No jobs from any source")


if __name__ == "__main__":
    main()
