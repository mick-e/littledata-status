# Littledata status

The public status page for [littledata.ai](https://littledata.ai):
**https://mick-e.github.io/littledata-status/**

## What it checks

About every 15 minutes a GitHub Actions run (`.github/workflows/probe.yml`) makes three read-only requests to littledata.ai, with no credentials, the way a visitor's browser does:

| Check | Request | Up when |
|---|---|---|
| Web app | `GET /en/login` | 200 with the app's page |
| API | `GET /health/live` | 200 with `{"status": "alive"}` |
| Database and cache | `GET /health` | 200 `healthy`, every sub-check healthy (200 `degraded` shows as degraded) |

A redirect counts as a failure, and a failed check is tried three times before it is recorded. Each run appends one entry to `data/history.json`, which keeps 90 days, and rebuilds `site/index.html` and `site/status.json` from it.

## Limits

- The checks run on GitHub Actions, which can start a scheduled run late or skip one. The page shows when it last checked, and says so when that is over an hour ago.
- When GitHub Actions itself is down, nothing is checked.
- The page reports availability as these three checks see it. It is not a service-level commitment.

## Development

`probe.py` uses the Python standard library only. Its tests need no network:

```
python3 -m unittest -v test_probe
```
