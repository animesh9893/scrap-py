"""The LinkedIn source against saved pages (tests/fixtures), with no network."""

import os
import time

import jobspy.linkedin

from search import Search
from sources import ROW_FIELDS, linkedin

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def page(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class Response:
    def __init__(self, text, url, status_code=200):
        self.text, self.url, self.status_code = text, url, status_code


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, params=None, **kwargs):
        self.urls.append(url)
        if "/jobs-guest/" in url:
            return Response(page("linkedin_search.html"), url)
        return Response(page("linkedin_job.html"), url)


def run(monkeypatch, search, known=frozenset()):
    session = FakeSession()
    monkeypatch.setattr(jobspy.linkedin, "create_session", lambda *a, **k: session)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return linkedin.run(search, known), session


SEARCH = Search(term="react developer", location="Bengaluru", name="react developer in Bengaluru", results=10,
                exclude=("lead",))


def test_rows_filters_and_known_jobs(monkeypatch):
    result, session = run(monkeypatch, SEARCH, known=frozenset({"4100000002"}))
    assert [r["job_id"] for r in result.rows] == ["4100000001"]
    assert result.seen == ["4100000002"]
    assert result.meta["counts"]["skipped"] == {"excluded_title": 1, "already_saved": 1}
    pages = [u for u in session.urls if "/jobs/view/" in u]
    assert pages == ["https://www.linkedin.com/jobs/view/4100000001"]     # the known job's page isn't read

    row = result.rows[0]
    assert list(row) == ROW_FIELDS
    assert row["source"] == "linkedin" and row["search_name"] == "react developer in Bengaluru"
    assert row["company"] == "Acme Labs" and row["company_slug"] == "acme"
    assert row["workplace"] == "Hybrid" and row["seniority"] == "Associate"
    assert row["employment_type"] == "Full-time"
    assert row["raw"]["applicants"] == 42 and row["raw"]["apply_type"] == "easy_apply"
    assert row["raw"]["experience_years"] == {"min": 3, "max": 5}


def test_source_location_is_what_linkedin_is_asked(monkeypatch):
    seen = {}
    real = linkedin.FilteredLinkedIn.scrape

    def spy(self, scraper_input):
        seen["location"] = scraper_input.location
        return real(self, scraper_input)

    monkeypatch.setattr(linkedin.FilteredLinkedIn, "scrape", spy)
    result, _ = run(monkeypatch, SEARCH.but(source_location="Bengaluru, Karnataka, India"))
    assert seen["location"] == "Bengaluru, Karnataka, India"
    assert result.rows[0]["raw"]["search"]["location"] == "Bengaluru"


def test_problems():
    assert linkedin.problems(SEARCH) == []
    assert len(linkedin.problems(SEARCH.but(sort="newest", job_type=("weekly",), company_ids=("google",)))) == 3
