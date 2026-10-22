"""What a run writes to OUT_DIR, which the workflow uploads as the run's artifact. For each search:
  jobs.json    {"meta": search, filters, counts, errors; "jobs": [rows]}: one row per job, shaped like the
               public.jobs table (sources.ROW_FIELDS), ready to upsert on (source, job_id)
  jobs.csv     the same rows, raw as a JSON string, for Excel / Sheets / COPY
  jobs.md      a Markdown table of the jobs (also shown on the run page)
  report.html  a self-contained page: search box, one card per job, expandable descriptions
A run of several searches (MODE=plan) puts each in its own folder and adds summary.md, a table of the searches.
"""

import csv
import html
import json
import os
import re
from datetime import datetime

from sources import ROW_FIELDS


def search_text(meta):
    s, f = meta["search"], meta["filters"]
    parts = [f"last {s['hours_old']}h" if s["hours_old"] else "any time", f"sorted by {s['sort']}"]
    parts += [f"{name.replace('_', ' ')}: {', '.join(map(str, value))}" for name, value in
              [("job_type", f["job_type"]), ("experience", f["experience"]), ("excluding", f["exclude"]),
               ("company_ids", s["company_ids"])] if value]
    if s["easy_apply"]:
        parts.append("Easy Apply only")
    return " · ".join(parts)


def skipped_text(meta):
    counts = meta["counts"]
    text = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts["skipped"].items())
    return f"skipped {text}" if text else ""


# ============ 3. Write ============
def write_json(out_dir, meta, rows):
    with open(os.path.join(out_dir, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "jobs": rows}, f, indent=1, ensure_ascii=False)


def write_csv(out_dir, rows):
    with open(os.path.join(out_dir, "jobs.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows({**r, "raw": json.dumps(r["raw"], ensure_ascii=False)} for r in rows)


DESCRIPTION_MAX = 12_000     # characters of each description in jobs.md (the rest is a link to the job)
SUMMARY_MAX = 900_000        # GitHub caps a step's run summary at 1 MiB


def markdown(meta, rows):
    """The jobs table, then each description in a collapsible block (jobs.md and the run page's summary)."""
    cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
    lines = [f"### {len(rows)} {meta['source_label']} jobs: {meta['search']['search_term']!r} in {meta['search']['location']!r}",
             "", search_text(meta), ""]
    if rows:
        lines += ["| Job | Company | Location | Workplace | Posted | Type | Seniority | Salary | Applicants |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| [{cell(r['title'])}]({r['url']}) | {cell(r['company'])} | {cell(r['location'])} | "
                  f"{cell(r['workplace'])} | {cell(r['raw']['posted_ago'] or r['posted_at'])} | "
                  f"{cell(r['employment_type'])} | {cell(r['seniority'])} | {cell(r['salary'])} | "
                  f"{cell(r['raw']['applicants_text'])} |" for r in rows]
    counts = meta["counts"]
    lines += ["", f"Looked at {counts['scanned']} jobs, kept {counts['kept']}"
                  f"{f', {n} with a hiring manager' if (n := counts.get('with_hiring_team')) else ''}."]
    if skipped_text(meta):
        lines[-1] += f" {skipped_text(meta).capitalize()}."
    if counts["stopped_at_scan_limit"]:
        lines[-1] += f" Stopped at the scan limit ({counts['scan_limit']}); loosen the filters or raise SCAN_LIMIT for more."
    if meta["error"]:
        lines += ["", f"**Error:** {cell(meta['error'])}"]
    if rows:
        lines += ["", "#### Descriptions", ""]
        size = sum(map(len, lines))
        for r in rows:
            description = r["description"] or "_No description: the job page couldn't be read._"
            if len(description) > DESCRIPTION_MAX:
                description = description[:DESCRIPTION_MAX].rsplit("\n", 1)[0] + f"\n\n… [full description]({r['url']})"
            block = [f"<details><summary><b>{html.escape(r['title'])}</b> · {html.escape(r['company'] or '')}</summary>",
                     "", description, "", "</details>", ""]
            if size + sum(map(len, block)) > SUMMARY_MAX:
                lines.append(f"_{len(rows) - rows.index(r)} more descriptions are in jobs.json / report.html._")
                break
            lines += block
            size += sum(map(len, block))
    return "\n".join(lines) + "\n"


SECTION_LABELS = {"requirements": "Requirements", "qualifications": "Qualifications"}


def bold(escaped_markdown):
    """**text** -> <b>text</b> in already-escaped Markdown (the report shows sections as text, not rendered HTML)."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped_markdown)


def report_html(meta, rows):
    e = lambda v: html.escape(str(v or ""))
    cards = []
    for r in rows:
        raw = r["raw"]
        chips = [c for c in (r["workplace"], r["employment_type"], r["seniority"], r["salary"], raw["industries"])
                 if c and c != "Not applicable"]
        text = " ".join(str(v or "") for v in (r["title"], r["company"], r["location"], r["workplace"],
                                               r["employment_type"], r["seniority"], raw["industries"])).lower()
        logo = f'<img class="logo" src="{e(raw["company_logo"])}" alt="" loading="lazy">' if raw["company_logo"] else \
               f'<div class="logo">{e((r["company"] or "?")[:1])}</div>'
        company = f'<a href="{e(r["company_url"])}" target="_blank" rel="noopener">{e(r["company"])}</a>' \
                  if r["company_url"] else e(r["company"])
        facts = [raw["applicants_text"], {"easy_apply": "Easy Apply", "company_site": "Apply on company site"}.get(raw["apply_type"])]
        if raw["experience_years"]:
            years = raw["experience_years"]
            facts.append(f"{years['min']}–{years['max']} yrs experience" if years["max"] else f"{years['min']}+ yrs experience")
        sections = "".join(f'<div class="md">{bold(e(body))}</div>'
                           for key, body in raw["sections"].items() if key in SECTION_LABELS)   # body starts with its heading
        desc = f'<details><summary>Description</summary><div class="desc">{raw["description_html"]}</div></details>' \
               if raw["description_html"] else '<p class="muted">No description (job page not readable).</p>'
        cards.append(f"""<article class="job" data-text="{e(text)}">
  {logo}
  <div class="body">
    <h2><a href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['title'])}</a></h2>
    <p class="meta">{company} · {e(r['location'])} · <span title="{e(r['posted_at'])}">{e(raw['posted_ago'] or r['posted_at'])}</span></p>
    <p class="chips">{''.join(f'<span>{e(c)}</span>' for c in chips)}</p>
    <p class="facts">{' · '.join(e(f) for f in facts if f)}</p>
    {f'<details><summary>Requirements &amp; qualifications</summary>{sections}</details>' if sections else ''}
    {desc}
  </div>
</article>""")
    counts, s = meta["counts"], meta["search"]
    error = f'<p class="error">{e(meta["error"])}</p>' if meta["error"] else ""
    if counts["stopped_at_scan_limit"]:
        error += (f'<p class="muted">Stopped at the scan limit ({counts["scan_limit"]}); loosen the filters or raise '
                  'SCAN_LIMIT for more.</p>')
    generated = datetime.fromisoformat(meta["generated_at"]).strftime("%d %b %Y, %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(meta['source_label'])} jobs</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2329; --muted:#5d6874; --line:#e1e5ea; --accent:#0a66c2; --chip:#eef3f8; --error:#b42318; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --card:#1a1f25; --text:#e6e9ed; --muted:#9aa5b1; --line:#2c333b; --accent:#70b5f9; --chip:#243040; --error:#f97066; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:24px 16px 48px; }}
header h1 {{ font-size:22px; margin:0 0 4px; }}
.muted, .sub {{ color:var(--muted); }} .sub {{ margin:0 0 4px; }}
.error {{ color:var(--error); }}
input {{ width:100%; margin:16px 0; padding:10px 12px; font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; }}
.job {{ display:flex; gap:14px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:12px; }}
.logo {{ flex:none; width:44px; height:44px; border-radius:8px; object-fit:contain; background:var(--chip); display:grid; place-items:center; font-weight:600; color:var(--muted); }}
.body {{ min-width:0; flex:1; }}
h2 {{ font-size:17px; margin:0; }} a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.meta {{ margin:2px 0 8px; color:var(--muted); }}
.chips {{ margin:0; display:flex; flex-wrap:wrap; gap:6px; }} .chips:empty, .facts:empty {{ display:none; }}
.chips span {{ background:var(--chip); border-radius:999px; padding:2px 10px; font-size:13px; }}
.facts {{ margin:8px 0 0; color:var(--muted); font-size:13px; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--accent); }}
.md {{ white-space:pre-wrap; overflow-wrap:anywhere; margin-top:8px; }}
.desc {{ margin-top:8px; overflow-wrap:anywhere; }} .desc p {{ margin:0 0 .6em; }}
.desc h1, .desc h2, .desc h3 {{ font-size:15px; }}
</style></head>
<body><main>
<header>
  <h1>{len(rows)} {e(meta['source_label'])} jobs: {e(s['search_term'])} in {e(s['location'])}</h1>
  <p class="sub">{e(search_text(meta))}</p>
  <p class="sub">Looked at {counts['scanned']}, kept {counts['kept']}{f' · {e(skipped_text(meta))}' if skipped_text(meta) else ''} · {e(generated)}</p>
  {error}
</header>
<input id="q" type="search" placeholder="Filter by title, company, location, workplace, seniority…" aria-label="Filter jobs">
<p class="muted" id="none" hidden>No jobs match.</p>
{chr(10).join(cards) or '<p class="muted">No jobs found.</p>'}
</main>
<script>
const q = document.getElementById("q"), jobs = [...document.querySelectorAll(".job")];
q.addEventListener("input", () => {{
  const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
  let shown = 0;
  for (const job of jobs) {{ job.hidden = !words.every(w => job.dataset.text.includes(w)); shown += !job.hidden; }}
  document.getElementById("none").hidden = shown > 0 || !jobs.length;
}});
</script>
</body></html>
"""


def write(out_dir, meta, rows):
    """All four files for one search. Returns jobs.md's text."""
    os.makedirs(out_dir, exist_ok=True)
    write_json(out_dir, meta, rows)
    write_csv(out_dir, rows)
    md = markdown(meta, rows)
    with open(os.path.join(out_dir, "jobs.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(report_html(meta, rows))
    return md


def summary(results, plan_note=""):
    """summary.md for a run of several searches: one line per search, with what was saved."""
    cell = lambda v: str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")
    lines = [f"### {len(results)} searches", ""] + ([plan_note, ""] if plan_note else [])
    lines += ["| Search | Source | Looked back | Looked at | Kept | New | Seen again | Folder | Problem |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        meta, saved = r["meta"], r.get("saved") or {}
        lines.append(f"| {cell(meta['search_name'])} | {cell(meta['source_label'])} | "
                     f"{cell(str(meta['search']['hours_old']) + 'h' if meta['search']['hours_old'] else 'any time')} | "
                     f"{meta['counts']['scanned']} | {meta['counts']['kept']} | {cell(saved.get('new'))} | "
                     f"{cell(saved.get('seen'))} | {cell(r['folder'])} | {cell(meta['error'])} |")
    return "\n".join(lines) + "\n"
