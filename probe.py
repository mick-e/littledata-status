#!/usr/bin/env python3
"""Check https://littledata.ai and rebuild the public status page.

Three read-only GETs, with no credentials, the way a visitor's browser reaches
the site:

    Web app             /en/login    200 with the app's page (its root element
                                     and its release tag)
    API                 /health/live 200 with {"status": "alive"}
    Database and cache  /health      200 "healthy" with every sub-check
                                     healthy; 200 "degraded" (the database
                                     answers, the cache does not) is degraded;
                                     anything else is down

A redirect is not followed and counts as a failure. A check that fails is
tried again (ATTEMPTS tries, RETRY_DELAY seconds apart), so one dropped
connection is not reported as an outage.

Each run appends one entry to data/history.json (kept for KEEP_DAYS days) and
writes site/index.html and site/status.json from the whole history.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import html
import http.client
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

BASE_URL = os.environ.get("STATUS_BASE_URL", "https://littledata.ai")
ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "history.json"
SITE = ROOT / "site"
KEEP_DAYS = 90
ATTEMPTS = 3
RETRY_DELAY = 10
TIMEOUT = 15
MAX_BODY = 1_000_000
STALE_MINUTES = 60

UP, DEGRADED, DOWN = "up", "degraded", "down"
CHECKS = (
    ("web", "Web app", "/en/login"),
    ("api", "API", "/health/live"),
    ("data", "Database and cache", "/health"),
)
_RANK = {UP: 0, DEGRADED: 1, DOWN: 2}


def worst(states: list[str]) -> str:
    return max(states, key=lambda s: _RANK[s]) if states else UP


def fetch(path: str) -> tuple[int, str, int]:
    """(HTTP status, body, milliseconds) for a GET of BASE_URL + path."""
    url = urllib.parse.urlsplit(BASE_URL)
    if url.scheme != "https" or not url.hostname:
        raise ValueError(f"not an https URL: {BASE_URL!r}")
    conn = http.client.HTTPSConnection(url.hostname, url.port, timeout=TIMEOUT)
    start = time.monotonic()
    try:
        conn.request(
            "GET",
            path,
            headers={
                "User-Agent": "littledata-status (+https://github.com/mick-e/littledata-status)",
                "Accept": "*/*",
            },
        )
        response = conn.getresponse()
        body = response.read(MAX_BODY).decode("utf-8", "replace")
        return response.status, body, round((time.monotonic() - start) * 1000)
    finally:
        conn.close()


def judge(key: str, status: int, body: str) -> tuple[str, str]:
    """The state one answer means, and a short reason when it is not up."""
    if key == "web":
        head = body.split("</head>", 1)[0]
        if (
            status == 200
            and '<meta name="release"' in head
            and '<div id="root"' in body
        ):
            return UP, ""
        return (
            DOWN,
            f"HTTP {status}, not the app's page" if status == 200 else f"HTTP {status}",
        )
    try:
        data = json.loads(body)
    except ValueError:
        return DOWN, f"HTTP {status}, not JSON"
    if not isinstance(data, dict):
        return DOWN, f"HTTP {status}, unexpected answer"
    if key == "api":
        if status == 200 and data.get("status") == "alive":
            return UP, ""
        return DOWN, f"HTTP {status}"
    checks = data.get("checks")
    sub_ok = (
        isinstance(checks, dict)
        and bool(checks)
        and all(
            isinstance(c, dict) and c.get("status") == "healthy"
            for c in checks.values()
        )
    )
    if status == 200 and data.get("status") == "healthy" and sub_ok:
        return UP, ""
    if status == 200 and data.get("status") == "degraded":
        return DEGRADED, "cache not answering"
    return DOWN, f"HTTP {status}"


def check(key: str, path: str) -> dict:
    """One check, tried up to ATTEMPTS times; the first up answer wins."""
    result = {"state": DOWN, "ms": None, "note": "not checked"}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            status, body, ms = fetch(path)
            state, note = judge(key, status, body)
            result = {"state": state, "ms": ms, "note": note}
        except (OSError, http.client.HTTPException, ValueError) as exc:
            result = {"state": DOWN, "ms": None, "note": type(exc).__name__}
        if result["state"] == UP:
            break
        if attempt < ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return result


def parse_time(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )


def fmt_time(stamp: str) -> str:
    return parse_time(stamp).strftime("%d %b %Y, %H:%M UTC")


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    entries = json.loads(HISTORY.read_text(encoding="utf-8"))
    return [e for e in entries if isinstance(e, dict) and "t" in e and "c" in e]


def prune(entries: list[dict], now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    return [e for e in entries if parse_time(e["t"]) >= cutoff]


def availability(entries: list[dict], key: str, since: dt.datetime) -> float | None:
    """Share of runs since *since* in which *key* was not down (None: no runs)."""
    states = [e["c"].get(key) for e in entries if parse_time(e["t"]) >= since]
    states = [s for s in states if s]
    if not states:
        return None
    return sum(s != DOWN for s in states) / len(states)


def daily(entries: list[dict], key: str, today: dt.date, days: int) -> list[dict]:
    """One cell per day, oldest first: the worst state seen and the share not down."""
    by_day: dict[dt.date, list[str]] = {}
    for e in entries:
        state = e["c"].get(key)
        if state:
            by_day.setdefault(parse_time(e["t"]).date(), []).append(state)
    cells = []
    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        states = by_day.get(day, [])
        cells.append(
            {
                "day": day.isoformat(),
                "runs": len(states),
                "state": worst(states) if states else None,
                "up": (sum(s != DOWN for s in states) / len(states))
                if states
                else None,
            }
        )
    return cells


def incidents(entries: list[dict], limit: int = 10) -> list[dict]:
    """Runs in a row where a check was not up, newest first."""
    found: list[dict] = []
    current: dict | None = None
    for e in entries:
        bad = {k: s for k, s in e["c"].items() if s != UP}
        if bad:
            if current is None:
                current = {"start": e["t"], "end": None, "checks": set(), "state": UP}
            current["checks"].update(bad)
            current["state"] = worst([current["state"], *bad.values()])
        elif current is not None:
            current["end"] = e["t"]
            found.append(current)
            current = None
    if current is not None:
        found.append(current)
    names = {key: name for key, name, _ in CHECKS}
    return [
        {
            "start": i["start"],
            "end": i["end"],
            "state": i["state"],
            "checks": sorted(names.get(k, k) for k in i["checks"]),
        }
        for i in reversed(found[-limit:])
    ]


def pct(value: float | None) -> str:
    if value is None:
        return "no data"
    text = f"{value * 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


_STATE_TEXT = {UP: "Operational", DEGRADED: "Degraded", DOWN: "Down"}
_BANNER = {
    UP: "All systems operational",
    DEGRADED: "Degraded performance",
    DOWN: "Service disruption",
}


def render(entries: list[dict], now: dt.datetime) -> tuple[str, dict]:
    """The page and the JSON summary, both from *entries* (newest last)."""
    latest = entries[-1]
    overall = worst(list(latest["c"].values()))
    first = entries[0]["t"]
    today = now.date()
    summary = {
        "checked_at": latest["t"],
        "status": overall,
        "history_starts": first,
        "checks": {},
    }
    rows = []
    for key, name, path in CHECKS:
        state = latest["c"].get(key, DOWN)
        windows = {
            label: availability(entries, key, now - dt.timedelta(days=days))
            for label, days in (("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90))
        }
        summary["checks"][key] = {
            "name": name,
            "path": path,
            "status": state,
            "response_ms": latest.get("ms", {}).get(key),
            "availability": {
                k: (round(v, 6) if v is not None else None) for k, v in windows.items()
            },
        }
        cells = []
        for cell in daily(entries, key, today, 90):
            cls = cell["state"] or "none"
            tip = (
                f'{cell["day"]}: no data'
                if cell["state"] is None
                else f'{cell["day"]}: {pct(cell["up"])} available, {cell["runs"]} checks'
            )
            cells.append(f'<span class="cell {cls}" title="{html.escape(tip)}"></span>')
        ms = latest.get("ms", {}).get(key)
        rows.append(
            f"""
      <section class="check">
        <div class="check-head">
          <h2>{html.escape(name)}</h2>
          <span class="pill {state}">{_STATE_TEXT[state]}</span>
        </div>
        <div class="bars" role="img" aria-label="{html.escape(name)}: availability per day, last 90 days">{"".join(cells)}</div>
        <div class="bar-legend"><span>90 days ago</span><span>today</span></div>
        <dl class="stats">
          <div><dt>24 hours</dt><dd>{pct(windows["24h"])}</dd></div>
          <div><dt>7 days</dt><dd>{pct(windows["7d"])}</dd></div>
          <div><dt>30 days</dt><dd>{pct(windows["30d"])}</dd></div>
          <div><dt>Response</dt><dd>{f"{ms} ms" if ms is not None else "none"}</dd></div>
        </dl>
      </section>"""
        )
    incident_items = []
    for inc in incidents(entries):
        span = (
            f'{fmt_time(inc["start"])} to {fmt_time(inc["end"])}'
            if inc["end"]
            else f'Since {fmt_time(inc["start"])} (ongoing)'
        )
        incident_items.append(
            f'<li><span class="pill {inc["state"]}">{_STATE_TEXT[inc["state"]]}</span> '
            f'<span>{html.escape(", ".join(inc["checks"]))}</span>'
            f'<span class="when">{html.escape(span)}</span></li>'
        )
    incident_html = (
        f'<ul class="incidents">{"".join(incident_items)}</ul>'
        if incident_items
        else '<p class="muted">No disruption recorded.</p>'
    )
    page = _TEMPLATE.format(
        overall=overall,
        banner=_BANNER[overall],
        checked=html.escape(fmt_time(latest["t"])),
        checked_iso=latest["t"],
        stale_minutes=STALE_MINUTES,
        rows="".join(rows),
        incidents=incident_html,
        since=html.escape(parse_time(first).strftime("%d %b %Y")),
        year=now.year,
    )
    return page, summary


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Littledata status</title>
<meta name="description" content="Live availability of littledata.ai, checked about every 15 minutes.">
<link rel="icon" href="logo.png">
<style>
  :root {{
    --bg: #f6f8fb; --card: #ffffff; --text: #0f172a; --muted: #5b6475; --line: #e3e8ef;
    --up: #1f9d55; --degraded: #c97a00; --down: #d03030; --none: #d5dbe3; --accent: #078ec3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b1120; --card: #131b2e; --text: #e6ebf3; --muted: #9aa5b8; --line: #243049;
      --up: #34c272; --degraded: #f0a42b; --down: #f05a5a; --none: #2c3850; --accent: #3db6e6;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px 48px; }}
  header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
  header img {{ height: 40px; width: auto; }}
  header a.site {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  .banner {{ margin: 24px 0; padding: 18px 20px; border-radius: 12px; color: #fff; font-size: 1.15rem;
    font-weight: 600; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  .banner.up {{ background: var(--up); }} .banner.degraded {{ background: var(--degraded); }}
  .banner.down {{ background: var(--down); }}
  .banner small {{ font-weight: 400; font-size: .9rem; opacity: .95; }}
  .stale {{ display: none; margin: -12px 0 24px; padding: 12px 16px; border-radius: 10px;
    border: 1px solid var(--degraded); color: var(--text); background: var(--card); font-size: .95rem; }}
  .check, .panel {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px 20px; margin-bottom: 16px; }}
  .check-head {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
  h2 {{ font-size: 1.05rem; margin: 0; }}
  .pill {{ display: inline-block; font-size: .8rem; font-weight: 600; padding: 2px 10px; border-radius: 999px;
    color: #fff; white-space: nowrap; }}
  .pill.up {{ background: var(--up); }} .pill.degraded {{ background: var(--degraded); }}
  .pill.down {{ background: var(--down); }}
  .bars {{ display: grid; grid-template-columns: repeat(90, 1fr); gap: 2px; margin: 14px 0 4px; height: 32px; }}
  .cell {{ border-radius: 2px; background: var(--none); }}
  .cell.up {{ background: var(--up); }} .cell.degraded {{ background: var(--degraded); }}
  .cell.down {{ background: var(--down); }}
  .bar-legend {{ display: flex; justify-content: space-between; color: var(--muted); font-size: .8rem; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 14px 0 0; }}
  .stats div {{ min-width: 0; }}
  .stats dt {{ color: var(--muted); font-size: .8rem; }}
  .stats dd {{ margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .incidents {{ list-style: none; margin: 0; padding: 0; }}
  .incidents li {{ display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; padding: 10px 0;
    border-top: 1px solid var(--line); }}
  .incidents li:first-child {{ border-top: 0; }}
  .when, .muted {{ color: var(--muted); font-size: .9rem; }}
  footer {{ color: var(--muted); font-size: .85rem; margin-top: 28px; }}
  footer p {{ margin: 6px 0; }}
  footer a {{ color: var(--accent); }}
  @media (max-width: 520px) {{
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .bars {{ gap: 1px; height: 28px; }}
  }}
</style>
</head>
<body>
<main>
  <header>
    <img src="logo.png" alt="Littledata" width="133" height="40">
    <a class="site" href="https://littledata.ai">littledata.ai</a>
  </header>
  <div class="banner {overall}" role="status">
    <span>{banner}</span>
    <small>Last checked {checked}</small>
  </div>
  <p class="stale" id="stale">No check has run for over {stale_minutes} minutes, so this page may be out of date. The checks run on GitHub Actions, which can delay or skip a scheduled run.</p>
  {rows}
  <section class="panel">
    <h2>Recent disruptions</h2>
    {incidents}
  </section>
  <footer>
    <p>Checked about every 15 minutes from GitHub Actions, outside Littledata's hosting: the app's sign-in page, the API's liveness endpoint, and the database and cache health endpoint. A day's bar shows the share of checks in which the service was not down. History starts {since}; times are UTC.</p>
    <p>&copy; {year} Littledata Limited &middot; <a href="status.json">status.json</a> &middot; <a href="https://github.com/mick-e/littledata-status">how this page is built</a></p>
  </footer>
</main>
<script>
  (function () {{
    var checked = Date.parse("{checked_iso}");
    if (!isNaN(checked) && Date.now() - checked > {stale_minutes} * 60 * 1000) {{
      document.getElementById("stale").style.display = "block";
    }}
  }})();
</script>
</body>
</html>
"""


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    entry = {"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": {}, "ms": {}, "note": {}}
    for key, _name, path in CHECKS:
        result = check(key, path)
        entry["c"][key] = result["state"]
        entry["ms"][key] = result["ms"]
        if result["note"]:
            entry["note"][key] = result["note"]
        print(f"{key:5} {result['state']:9} {result['ms']} ms {result['note']}")
    if not entry["note"]:
        del entry["note"]
    entries = prune(load_history() + [entry], now)
    page, summary = render(entries, now)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(
        json.dumps(entries, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (SITE / "status.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
