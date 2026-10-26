"""Writes the job feed: an XML file of the jobs found lately, in the format job aggregators such as Jooble read
(<jobs><job id="…"><link/><name/><region/><description/>…</job></jobs>). Each job links to its page on the site
(job.html?id=…), so the visitors they send land on krawlr.xyz.

.github/workflows/feed.yml runs it every hour and pushes the file to the "feed" branch; the site serves that file at
https://krawlr.xyz/jobs.xml (netlify.toml). Run it by hand with:
  DATABASE_URL=postgresql://… python feed.py out/jobs.xml

FEED_HOURS (default 48) is how far back it goes, by when the job was first found.
"""

import html
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg

SITE = os.environ.get("SITE_URL", "https://krawlr.xyz").rstrip("/")
HOURS = int(os.environ.get("FEED_HOURS") or 48)
DESCRIPTION_CHARS = 3000   # longer descriptions are cut at a paragraph and end with a link to the full one
KEEP_DAYS = 2              # the database drops a job 48 hours after it was posted (delete_old_jobs in schema.sql)

QUERY = """
    SELECT job_id, title, company, location, workplace, salary, description, posted_at, employment_type,
           first_seen_at, last_seen_at
    FROM jobs
    WHERE status <> 'skipped' AND first_seen_at > now() - make_interval(hours => %s)
    ORDER BY first_seen_at DESC
"""

JOB_TYPES = {"Full-time": "full-time", "Part-time": "part-time", "Contract": "contract",
             "Temporary": "temporary", "Internship": "internship", "Volunteer": "volunteer"}


def cdata(text):
    """text as a CDATA section, which can't contain "]]>" or characters XML doesn't allow."""
    text = re.sub(r"[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]", "", str(text))
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def posted_day(posted_at, first_seen_at):
    """The day the job was posted: posted_at is an ISO minute timestamp or a date (YYYY-MM-DD) as text."""
    try:
        return date.fromisoformat((posted_at or "")[:10])
    except ValueError:
        return first_seen_at.date()


def ddmmyyyy(d):
    return d.strftime("%d.%m.%Y")


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)


def to_html(markdown, link):
    """The description (Markdown, as the scraper saves it) as simple HTML: paragraphs, bold, italics and lists.
    Cut at about DESCRIPTION_CHARS, at a paragraph, with a link to the job page for the rest."""
    markdown = (markdown or "").replace("\r\n", "\n").strip()
    cut = len(markdown) > DESCRIPTION_CHARS
    if cut:
        end = markdown.rfind("\n\n", 0, DESCRIPTION_CHARS)
        markdown = markdown[:end if end > DESCRIPTION_CHARS // 2 else DESCRIPTION_CHARS].rstrip()
    out = []
    for block in re.split(r"\n\s*\n", markdown):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if all(re.match(r"[-*•]\s+", l) for l in lines):
            out.append("<ul>" + "".join(f"<li>{inline(re.sub(r'^[-*•]\s+', '', l))}</li>" for l in lines) + "</ul>")
        else:
            heading = re.match(r"#+\s+(.*)", lines[0])
            if heading and len(lines) == 1:
                out.append(f"<p><b>{inline(heading.group(1))}</b></p>")
            else:
                out.append("<p>" + "<br>".join(inline(l) for l in lines) + "</p>")
    if cut:
        out.append(f'<p><a href="{html.escape(link)}">Read the full description</a></p>')
    return "\n".join(out)


def job_xml(row):
    job_id, title, company, location, workplace, salary, description, posted_at, employment_type, \
        first_seen_at, last_seen_at = row
    link = f"{SITE}/job.html?id={job_id}"
    posted = posted_day(posted_at, first_seen_at)
    region = location or ""
    if workplace == "remote" and "remote" not in region.lower():
        region = f"{region} (Remote)" if region else "Remote"
    parts = [f'<job id="{html.escape(str(job_id))}">',
             f"<link>{cdata(link)}</link>",
             f"<name>{cdata(title or '')}</name>",
             f"<region>{cdata(region)}</region>",
             f"<description>{cdata(to_html(description, link))}</description>",
             f"<company>{cdata(company or '')}</company>",
             f"<pubdate>{ddmmyyyy(posted)}</pubdate>",
             f"<updated>{ddmmyyyy(last_seen_at.date())}</updated>",
             f"<expire>{ddmmyyyy(posted + timedelta(days=KEEP_DAYS))}</expire>"]
    if salary:
        parts.append(f"<salary>{cdata(salary)}</salary>")
    if employment_type in JOB_TYPES:
        parts.append(f"<jobtype>{cdata(JOB_TYPES[employment_type])}</jobtype>")
    parts.append("</job>")
    return "\n".join(parts) + "\n"


def write_feed(rows, out):
    """Writes the feed for rows (the QUERY's columns) to the open text file out; returns how many jobs."""
    n = 0
    out.write('<?xml version="1.0" encoding="utf-8"?>\n')
    out.write(f"<!-- Jobs from {SITE}, generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC -->\n<jobs>\n")
    for row in rows:
        if row[1] and row[0]:
            out.write(job_xml(row))
            n += 1
    out.write("</jobs>\n")
    return n


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "jobs.xml"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sslmode = "disable" if os.environ.get("DATABASE_SSL") == "false" else "require"
    # Reads only: no db.connect(), which would apply the schema.
    with psycopg.connect(os.environ["DATABASE_URL"], sslmode=sslmode) as conn, conn.cursor() as cur:
        cur.execute(QUERY, (HOURS,))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as out:
            n = write_feed(cur, out)
    os.replace(tmp, path)
    print(f"Wrote {n} jobs to {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
