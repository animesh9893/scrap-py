import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import feed

SEEN = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)


def row(**kw):
    r = dict(job_id="4321", title="Backend Engineer", company="Acme & Co", location="Bengaluru, Karnataka, India",
             workplace=None, salary=None, description="**About us**\n\nWe build things.\n\n- Python\n- SQL",
             posted_at="2026-10-25T18:04", employment_type="Full-time", first_seen_at=SEEN, last_seen_at=SEEN)
    r.update(kw)
    return tuple(r.values())


def parse(rows):
    out = io.StringIO()
    n = feed.write_feed(rows, out)
    return n, ET.fromstring(out.getvalue().encode())


def test_job_fields():
    n, root = parse([row()])
    assert n == 1
    job = root.find("job")
    assert job.get("id") == "4321"
    assert job.findtext("link") == "https://krawlr.xyz/job.html?id=4321"
    assert job.findtext("name") == "Backend Engineer"
    assert job.findtext("company") == "Acme & Co"
    assert job.findtext("region") == "Bengaluru, Karnataka, India"
    assert job.findtext("pubdate") == "25.10.2026"
    assert job.findtext("updated") == "26.10.2026"
    assert job.findtext("expire") == "27.10.2026"
    assert job.findtext("jobtype") == "full-time"
    assert job.find("salary") is None
    assert job.findtext("description") == "<p><b>About us</b></p>\n<p>We build things.</p>\n<ul><li>Python</li><li>SQL</li></ul>"


def test_odd_values():
    long = "\n\n".join(["word " * 200] * 20)
    n, root = parse([row(posted_at="2026-10-24", workplace="remote", location="India", salary="₹10L ]]> yr",
                         employment_type="other", description=long + "\x01"),
                     row(title=None)])
    assert n == 1
    job = root.find("job")
    assert job.findtext("pubdate") == "24.10.2026"
    assert job.findtext("region") == "India (Remote)"
    assert job.findtext("salary") == "₹10L ]]> yr"
    assert job.find("jobtype") is None
    desc = job.findtext("description")
    assert len(desc) < feed.DESCRIPTION_CHARS + 500 and desc.endswith("Read the full description</a></p>")


def test_bad_posted_at_uses_first_seen():
    _, root = parse([row(posted_at="2 days ago")])
    assert root.find("job").findtext("pubdate") == "26.10.2026"
