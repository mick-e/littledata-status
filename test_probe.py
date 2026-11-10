"""Tests for probe.py: how each answer is judged, and what the page says about a history.

Run before every probe (the workflow stops if one fails), with no network.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest

import probe

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
APP_PAGE = (
    '<html><head><meta name="release" content="abc"></head>'
    '<body><div id="root"></div></body></html>'
)
HEALTHY = json.dumps(
    {
        "status": "healthy",
        "checks": {"database": {"status": "healthy"}, "redis": {"status": "healthy"}},
    }
)


def entry(minutes_ago: int, web="up", api="up", data="up") -> dict:
    t = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "t": t,
        "c": {"web": web, "api": api, "data": data},
        "ms": {"web": 200, "api": 90, "data": 80},
    }


class JudgeTest(unittest.TestCase):
    def test_the_app_page_is_up(self) -> None:
        self.assertEqual(probe.judge("web", 200, APP_PAGE)[0], probe.UP)

    def test_a_page_without_the_release_tag_in_its_head_is_down(self) -> None:
        body = (
            '<html><head></head><body><div id="root"></div><meta name="release"></body>'
        )
        self.assertEqual(probe.judge("web", 200, body)[0], probe.DOWN)

    def test_json_where_the_app_page_should_be_is_down(self) -> None:
        self.assertEqual(
            probe.judge("web", 200, '{"error": "Dashboard not found"}')[0], probe.DOWN
        )

    def test_liveness(self) -> None:
        self.assertEqual(probe.judge("api", 200, '{"status": "alive"}')[0], probe.UP)
        self.assertEqual(probe.judge("api", 503, '{"status": "alive"}')[0], probe.DOWN)
        self.assertEqual(probe.judge("api", 200, "<html>")[0], probe.DOWN)

    def test_health_has_three_answers(self) -> None:
        self.assertEqual(probe.judge("data", 200, HEALTHY)[0], probe.UP)
        degraded = json.dumps(
            {"status": "degraded", "checks": {"redis": {"status": "unhealthy"}}}
        )
        self.assertEqual(probe.judge("data", 200, degraded)[0], probe.DEGRADED)
        self.assertEqual(
            probe.judge("data", 503, json.dumps({"status": "unhealthy"}))[0], probe.DOWN
        )

    def test_healthy_over_a_failing_sub_check_is_down(self) -> None:
        body = json.dumps(
            {"status": "healthy", "checks": {"redis": {"status": "unhealthy"}}}
        )
        self.assertEqual(probe.judge("data", 200, body)[0], probe.DOWN)
        self.assertEqual(
            probe.judge("data", 200, json.dumps({"status": "healthy"}))[0], probe.DOWN
        )

    def test_a_redirect_is_down(self) -> None:
        self.assertEqual(probe.judge("web", 301, "")[0], probe.DOWN)
        self.assertEqual(probe.judge("api", 302, "")[0], probe.DOWN)


class HistoryTest(unittest.TestCase):
    def test_availability_counts_degraded_as_available(self) -> None:
        entries = [entry(30, data="degraded"), entry(15, data="down"), entry(0)]
        since = NOW - dt.timedelta(days=1)
        self.assertAlmostEqual(probe.availability(entries, "data", since), 2 / 3)
        self.assertEqual(probe.availability(entries, "web", since), 1.0)
        self.assertIsNone(probe.availability([], "web", since))

    def test_an_incident_runs_from_the_first_bad_check_to_the_first_good_one(
        self,
    ) -> None:
        entries = [
            entry(60),
            entry(45, api="down"),
            entry(30, api="down", web="down"),
            entry(15),
            entry(0),
        ]
        found = probe.incidents(entries)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["start"], entries[1]["t"])
        self.assertEqual(found[0]["end"], entries[3]["t"])
        self.assertEqual(found[0]["checks"], ["API", "Web app"])
        self.assertEqual(found[0]["state"], probe.DOWN)

    def test_an_ongoing_incident_has_no_end(self) -> None:
        found = probe.incidents([entry(15), entry(0, data="degraded")])
        self.assertIsNone(found[0]["end"])
        self.assertEqual(found[0]["state"], probe.DEGRADED)

    def test_daily_cells_cover_the_window_and_mark_days_without_data(self) -> None:
        cells = probe.daily([entry(0), entry(30, web="down")], "web", NOW.date(), 90)
        self.assertEqual(len(cells), 90)
        self.assertEqual(cells[-1]["day"], NOW.date().isoformat())
        self.assertEqual(cells[-1]["state"], probe.DOWN)
        self.assertEqual(cells[-1]["up"], 0.5)
        self.assertIsNone(cells[0]["state"])

    def test_prune_drops_entries_older_than_the_window(self) -> None:
        old = entry(60 * 24 * (probe.KEEP_DAYS + 1))
        self.assertEqual(probe.prune([old, entry(0)], NOW), [entry(0)])


class RenderTest(unittest.TestCase):
    def test_the_page_says_what_the_latest_run_found(self) -> None:
        page, summary = probe.render([entry(15), entry(0, data="degraded")], NOW)
        self.assertIn("Degraded performance", page)
        self.assertEqual(summary["status"], probe.DEGRADED)
        self.assertEqual(summary["checks"]["data"]["status"], probe.DEGRADED)
        self.assertIn("10 Nov 2026, 12:00 UTC", page)

    def test_all_up_reads_operational_and_lists_no_disruption(self) -> None:
        page, summary = probe.render([entry(0)], NOW)
        self.assertIn("All systems operational", page)
        self.assertIn("No disruption recorded.", page)
        self.assertEqual(summary["checks"]["web"]["availability"]["24h"], 1.0)

    def test_the_page_carries_nothing_but_the_checks(self) -> None:
        page, _ = probe.render([entry(0)], NOW)
        for name in ("Web app", "API", "Database and cache"):
            self.assertIn(name, page)
        self.assertNotIn("{", page.split("<script>")[0].split("</style>")[1])


if __name__ == "__main__":
    unittest.main()
