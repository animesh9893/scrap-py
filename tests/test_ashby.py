"""sources/ashby.py on a saved board (tests/fixtures/ashby_board.json, shaped like Ashby's posting API answer)."""

import json
import os
from datetime import datetime, timezone

import pytest

from sources import ashby

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ashby_board.json")


def board():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_rows_keep_recent_listed_tech_jobs():
    rows, counts = ashby.rows(board(), "Acme", "acme", NOW, max_age_days=30)
    assert [r["title"] for r in rows] == ["Backend Software Engineer", "Frontend Engineer (React)"]
    assert counts == {"listed": 6, "kept": 2, "skipped": {"not_tech": 2, "too_old": 1, "unlisted": 1}}


def test_row_fields():
    first, second = ashby.rows(board(), "Acme", "acme", NOW)[0]
    assert first["source"] == "ashby" and first["job_id"] == "240d459b-696d-43eb-8497-fab3e56ecd9b"
    assert first["company"] == "Acme" and first["company_url"] == "https://jobs.ashbyhq.com/acme"
    assert first["location"] == "San Francisco, California, United States +2 more"
    assert first["workplace"] == "Hybrid" and second["workplace"] == "Remote"
    assert first["employment_type"] == "Full-time"
    assert first["salary"] == "$150K - $200K" and second["salary"] is None      # second hides its pay
    assert first["posted_at"] == "2026-10-20T10:03:20.653+00:00"
    assert first["apply_url"].endswith("/application")
    assert first["raw"]["experience_years"] == {"min": 3, "max": 5}
    assert first["raw"]["country"] == "United States" and first["raw"]["board"] == "acme"
    assert set(first) == set(__import__("sources").ROW_FIELDS)


def test_any_age():
    rows, _ = ashby.rows(board(), "Acme", "acme", NOW, max_age_days=0)
    assert "Data Engineer" in [r["title"] for r in rows]


@pytest.mark.parametrize("title, tech", [
    ("Software Engineer, Backend", True), ("Senior iOS Developer", True), ("Data Scientist", True),
    ("Site Reliability Engineer (SRE)", True), ("Account Executive", False), ("Sales Engineer", False),
    ("Forward Deployed Engineer", False), ("Technical Recruiter", False), ("Office Manager", False),
    ("Customer Support Engineer", False), ("Product Marketing Manager", False),
])
def test_is_tech(title, tech):
    assert ashby.is_tech(title) is tech


def test_board_slug():
    assert ashby.board_slug("https://jobs.ashbyhq.com/openai") == "openai"
    assert ashby.board_slug("https://jobs.ashbyhq.com/Air-Apps/") == "Air-Apps"
    assert ashby.board_slug("https://boards.greenhouse.io/x") is None


class Response:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response, self.urls = response, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.response


def test_fetch():
    s = Session(Response(200, {"jobs": []}))
    assert ashby.fetch(s, "openai") == {"jobs": []}
    assert s.urls == ["https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true"]
    with pytest.raises(ashby.NotFound):
        ashby.fetch(Session(Response(404)), "gone")
