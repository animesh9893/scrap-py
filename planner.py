"""Which searches a run does (MODE=plan): every active keyword × active location × enabled source, minus the
exclusions, that is due, most overdue first, cut to the run's budget. MODE=sweep takes every pair, due or not.

  excluded     the location is in the keyword's excluded_location_ids, or the source is in the location's or the
               keyword's excluded_sources
  due          not tried for interval_hours (the smaller of the keyword's and the location's, else
               DEFAULT_INTERVAL_HOURS), with SLACK to spare so a scheduled run that starts early doesn't skip it
  looks back   since the pair's last successful run plus an hour of overlap, at most the keyword's hours_old (else
               HOURS_OLD); a pair never run looks back the full hours_old
  order        never run first (lower priority first: keyword's + location's), then most overdue weighted by
               priority (overdue × 200 / priority), so a pair with a high priority number still gets its turn when
               there are more due pairs than a run takes, just less often
  term         the keyword, plus the location's source_params.<source>.term_suffix if it has one ("remote" for a
               "Remote - US" location searched as "United States")

plan() reads the lists and the runs table; build() is the same without a database, for tests and `MODE=plan-only`.
"""

import math
from dataclasses import dataclass
from datetime import timedelta

SLACK = timedelta(minutes=5)


@dataclass
class Planned:
    search: object          # search.Search
    last_ok: object         # when the pair last succeeded (None: never)
    overdue: float          # time since the last try / interval; inf when never tried


def build(keywords, locations, sources, last_tried, last_ok, defaults, now, *, default_interval_hours, max_tasks,
          due_only=True):
    """keywords / locations: dict rows of search_keywords / locations (active ones). sources: enabled source names.
    last_tried / last_ok: {(source, keyword_id, location_id): finished_at}. defaults: the Search the environment
    describes (distance, results, sort, scan limit, proxies, hours_old, exclude). due_only=False (MODE=sweep): every
    pair, due or not, each looking back the full hours_old. Returns (planned, due_count)."""
    due = []
    for kw in keywords:
        for loc in locations:
            if loc["id"] in (kw["excluded_location_ids"] or []):
                continue
            for source in sources:
                if source in (loc["excluded_sources"] or []) or source in (kw["excluded_sources"] or []):
                    continue
                key = (source, kw["id"], loc["id"])
                hours = min([h for h in (kw["interval_hours"], loc["interval_hours"]) if h] or [default_interval_hours])
                interval = timedelta(hours=hours)
                tried = last_tried.get(key)
                if due_only and tried and now - tried < interval - SLACK:
                    continue
                full = kw["hours_old"] or defaults.hours_old
                ok = last_ok.get(key)
                hours_old = (min(full, math.ceil((now - ok).total_seconds() / 3600) + 1) if due_only and ok and full
                             else full)
                params = (loc["source_params"] or {}).get(source) or {}
                term = " ".join(filter(None, [kw["keyword"], params.get("term_suffix")]))
                search = defaults.but(
                    source=source, term=term, location=loc["name"], name=f"{kw['keyword']} in {loc['name']}",
                    hours_old=hours_old, results=kw["max_results"] or defaults.results,
                    scan_limit=max(defaults.scan_limit, (kw["max_results"] or 0) * 5),
                    exclude=tuple(kw["exclude_title_words"] or defaults.exclude),
                    experience=tuple(kw["experience"] or defaults.experience),
                    category=kw["category"], keyword_id=kw["id"], location_id=loc["id"],
                    source_location=params.get("location"), company_ids=(), easy_apply=False)
                due.append((kw["priority"] + loc["priority"],
                            Planned(search, ok, (now - tried) / interval if tried else math.inf)))
    due.sort(key=lambda d: (d[1].overdue != math.inf,
                            d[0] if d[1].overdue == math.inf else -d[1].overdue * 200 / max(d[0], 1)))
    return [p for _, p in due[:max_tasks]], len(due)


def plan(cur, available_sources, defaults, now, *, default_interval_hours, max_tasks, due_only=True):
    """build() from the database: the active lists, the enabled sources this code has, and scrap-py's past runs."""
    def rows(sql, params=()):
        cur.execute(sql, params)
        names = [c.name for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    keywords = rows("SELECT * FROM search_keywords WHERE is_active ORDER BY priority, id")
    locations = rows("SELECT * FROM locations WHERE is_active ORDER BY priority, id")
    sources = [r["id"] for r in rows("SELECT id FROM sources WHERE enabled AND id = ANY(%s) ORDER BY id",
                                     (list(available_sources),))]
    runs = rows("""SELECT source, keyword_id, location_id, MAX(finished_at) AS tried,
                          MAX(finished_at) FILTER (WHERE status = 'SUCCEEDED') AS ok
                   FROM runs WHERE actor = 'scrap-py' AND finished_at IS NOT NULL AND keyword_id IS NOT NULL
                   GROUP BY source, keyword_id, location_id""")
    key = lambda r: (r["source"], r["keyword_id"], r["location_id"])
    planned, due = build(keywords, locations, sources, {key(r): r["tried"] for r in runs},
                         {key(r): r["ok"] for r in runs if r["ok"]}, defaults, now,
                         default_interval_hours=default_interval_hours, max_tasks=max_tasks, due_only=due_only)
    return planned, due, dict(keywords=len(keywords), locations=len(locations), sources=sources)
