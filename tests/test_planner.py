from datetime import datetime, timedelta, timezone

import planner
from search import Search

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
DEFAULTS = Search(term="", location="", name="", hours_old=24, results=30, scan_limit=150)


def kw(id, keyword, **extra):
    base = dict(id=id, keyword=keyword, category=None, excluded_location_ids=[], excluded_sources=[],
                exclude_title_words=[], experience=[], hours_old=None, max_results=None, interval_hours=None,
                priority=100)
    return {**base, **extra}


def loc(id, name, **extra):
    base = dict(id=id, name=name, source_params={}, excluded_sources=[], interval_hours=None, priority=100)
    return {**base, **extra}


def build(keywords, locations, sources=("linkedin",), tried=None, ok=None, max_tasks=10):
    return planner.build(keywords, locations, list(sources), tried or {}, ok or {}, DEFAULTS, NOW,
                         default_interval_hours=2, max_tasks=max_tasks)


def names(planned):
    return [p.search.name for p in planned]


def test_every_pair_minus_exclusions():
    keywords = [kw(1, "react", excluded_location_ids=[2]), kw(2, "java", excluded_sources=["naukri"])]
    locations = [loc(1, "Pune"), loc(2, "Noida", excluded_sources=["naukri"])]
    planned, due = build(keywords, locations, sources=("linkedin", "naukri"))
    pairs = sorted((p.search.source, p.search.name) for p in planned)
    assert pairs == [("linkedin", "java in Noida"), ("linkedin", "java in Pune"),
                     ("linkedin", "react in Pune"), ("naukri", "react in Pune")]
    assert due == 4


def test_due_lookback_and_order():
    keywords = [kw(1, "react"), kw(2, "java", interval_hours=6, hours_old=48), kw(3, "go", priority=50)]
    tried = {("linkedin", 1, 1): NOW - timedelta(minutes=30),       # not due (every 2h)
             ("linkedin", 2, 1): NOW - timedelta(hours=7),          # due (every 6h)
             ("linkedin", 3, 1): NOW - timedelta(hours=3)}          # due, failed last time
    ok = {("linkedin", 2, 1): NOW - timedelta(hours=7, minutes=10),
          ("linkedin", 3, 1): NOW - timedelta(hours=30)}
    planned, due = build(keywords + [kw(4, "rust")], [loc(1, "Pune")], tried=tried, ok=ok)
    assert names(planned) == ["rust in Pune", "go in Pune", "java in Pune"]     # never run, then priority
    hours = {p.search.term: p.search.hours_old for p in planned}
    assert hours == {"rust": 24, "go": 24, "java": 9}      # since the last success + 1h, at most hours_old
    assert due == 3


def test_budget_and_keyword_settings():
    keywords = [kw(1, "react", exclude_title_words=["senior"], max_results=50, category="frontend")]
    locations = [loc(1, "Bengaluru", source_params={"linkedin": {"location": "Bengaluru, Karnataka, India"}}),
                 loc(2, "Pune"), loc(3, "Chennai")]
    planned, due = build(keywords, locations, max_tasks=2)
    assert len(planned) == 2 and due == 3
    s = planned[0].search
    assert (s.exclude, s.results, s.scan_limit, s.category) == (("senior",), 50, 250, "frontend")
    assert (s.keyword_id, s.location_id, s.where) == (1, 1, "Bengaluru, Karnataka, India")
