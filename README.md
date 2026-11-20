# Instantly Domain Burn Detector — v1.0.0

A Python CLI tool that pulls daily analytics from **Instantly API V2** into **SQLite**, detects burned domains, and outputs color-coded CLI reports.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up your API key
cp .env.example .env
# Edit .env → set INSTANTLY_API_KEY=your_key_here

# 3. Run the daily ETL
python -m domain_tracker run

# 4. View domain health report
python -m domain_tracker report
```

## CLI Commands

| Command | Description |
|---|---|
| `python -m domain_tracker run` | Full ETL pipeline + report (requires API key) |
| `python -m domain_tracker report` | Show domain health report (reads from DB) |
| `python -m domain_tracker detail -d example.com` | Per-mailbox drill-down for a domain |
| `python -m domain_tracker trend -d example.com` | Daily trend over time |
| `python -m domain_tracker accounts` | List all tracked accounts |
| `python -m domain_tracker --help` | Show all commands |

### Options

```
run:
  --skip-campaigns     Skip campaign mapping refresh (faster)
  --full-backfill      Pull from BACKFILL_START_DATE (default: 2026-01-01)
  --no-report          Skip showing report after ETL

report / detail:
  --window / -w N      Override lookback window (default: 7 days)
  --domain / -d NAME   Filter to a specific domain
  --min-date / -f      Start date (YYYY-MM-DD)
  --max-date / -t      End date (YYYY-MM-DD)

trend:
  --days N             Number of days to show (default: 30)

accounts:
  --domain / -d NAME   Filter to a specific domain
```

## Burn Detection

| Condition | Classification |
|---|---|
| Reply rate < 1.0% (7-day) | 🔥 **BURNED** |
| Bounce rate > 8.0% | 🔥 **BURNED** |
| Reply rate < 2.0% | ⚠ **AT_RISK** |
| Bounce rate > 5.0% | ⚠ **AT_RISK** |
| Reply rate < 30% of best domain AND < 3.0% | 🚩 **UNDERPERFORMING** |
| None of the above | ✅ **HEALTHY** |

Minimum 50 sent to qualify. Thresholds are configurable in `.env`.

## Project Structure

```
instantly-domain-tracker/
├── domain_tracker/          # Python package
│   ├── __init__.py          # Package metadata
│   ├── __main__.py          # python -m domain_tracker entry
│   ├── main.py              # Click CLI (run, report, detail, trend, accounts)
│   ├── config.py            # .env loader + typed settings
│   ├── db.py                # SQLite schema + UPSERT helpers
│   ├── api.py               # Instantly API V2 client
│   ├── etl.py               # Daily pipeline
│   ├── campaign_etl.py      # Campaign analytics/account-metrics sync
│   ├── analytics.py         # Domain stats + burn classification
│   └── report.py            # Rich CLI table formatting
├── dashboard/                # Flask web dashboard
│   ├── app.py
│   ├── static/
│   └── templates/
├── deploy/
│   ├── domain-tracker-sync.service  # systemd oneshot (runs run_daily.sh)
│   ├── domain-tracker-sync.timer    # daily 7:00 AM IST schedule
│   └── logrotate.conf
├── scripts/
│   └── run_daily.sh         # daily ETL entry point (invoked by the timer)
├── tests/                   # Pytest tests
│   └── test_all.py
├── data/                    # SQLite DBs, per-user (gitignored)
├── .env.example
├── requirements.txt         # runtime + dashboard deps
├── requirements-dev.txt     # + pytest
└── README.md
```

## Configuration

Copy `.env.example` to `.env` and set your Instantly API key.

```bash
cp .env.example .env
# Edit .env → set INSTANTLY_API_KEY=your_key_here
```

All settings in `.env`:

| Variable | Default | Description |
|---|---|---|
| `INSTANTLY_API_KEY` | — | Your Instantly API key |
| `DATABASE_PATH` | `./data/instantly_tracker.db` | SQLite DB path |
| `BURN_REPLY_RATE_THRESHOLD` | 1.0 | Reply rate % → BURNED |
| `AT_RISK_REPLY_RATE_THRESHOLD` | 2.0 | Reply rate % → AT_RISK |
| `BURN_BOUNCE_RATE_THRESHOLD` | 8.0 | Bounce rate % → BURNED |
| `AT_RISK_BOUNCE_RATE_THRESHOLD` | 5.0 | Bounce rate % → AT_RISK |
| `LOOKBACK_DAYS` | 7 | Analysis window |
| `MIN_VOLUME_THRESHOLD` | 50 | Min sent to qualify |
| `BACKFILL_START_DATE` | 2026-01-01 | First-run backfill start |

## Daily Sync Schedule

`scripts/run_daily.sh` runs the ETL inside the Docker container, splits output
into `data/cron.log` (stdout) / `data/cron.err` (stderr), and POSTs to
`ALERT_WEBHOOK_URL` (if set in `.env`) on failure.

In production it's scheduled by a **systemd timer** (`deploy/domain-tracker-sync.timer`)
at 7:00 AM IST — systemd's `OnCalendar` supports real timezones
(`*-*-* 07:00:00 Asia/Kolkata`). Don't use cron with `CRON_TZ` for this:
Ubuntu's cron silently ignores `CRON_TZ` and runs the job at 9 AM in the
system timezone instead. See step 5 of Production Deployment below.

Set up log rotation so `cron.log`/`cron.err` don't grow forever — see `deploy/logrotate.conf`.

### macOS Launchd Alternative (recommended for macOS)

If crontab gives "Operation not permitted" on macOS, use launchd:

Create `~/Library/LaunchAgents/com.instantly.domain-tracker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.instantly.domain-tracker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>domain_tracker</string>
        <string>run</string>
        <string>--skip-campaigns</string>
        <string>--skip-campaign-sync</string>
        <string>--no-report</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/instantly-domain-tracker</string>
    <key>StandardOutPath</key>
    <string>/path/to/instantly-domain-tracker/data/cron.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/instantly-domain-tracker/data/cron.log</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.instantly.domain-tracker.plist
```

## Dashboard

```bash
# From the repo root — all deps (CLI + dashboard) live in one requirements.txt
pip install -r requirements.txt
cd dashboard/
python3 app.py
# Opens at http://localhost:8000
```

No login is required locally. In production, set `DASHBOARD_PASSWORD` (see below) to require a password.

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Production Deployment (VPS)

Runs as a Docker container behind an existing shared Caddy reverse proxy
(no host port published — only reachable via Caddy on the `shared_proxy`
Docker network). You can optionally wire up a GitHub Actions SSH workflow to
redeploy on every push to `main` (see step 7 below).

```bash
# 1. Clone
git clone git@github.com:<you>/instantly-domain-tracker.git /opt/instantly-domain-tracker
cd /opt/instantly-domain-tracker

# 2. Configure
cp .env.example .env
# Edit .env:
#   INSTANTLY_API_KEY=...
#   DASHBOARD_PASSWORD=<a password only you know>
#   FLASK_SECRET_KEY=<python3 -c "import secrets; print(secrets.token_hex(32))">
#   ALERT_WEBHOOK_URL=<optional Slack/Discord webhook for cron failures>

# 3. Bring it up (also creates the shared_proxy network if missing)
docker network inspect shared_proxy >/dev/null 2>&1 || docker network create shared_proxy
docker compose up -d --build

# 4. Add a Caddy site block (in your Caddy proxy's Caddyfile) — auto-provisions
#    TLS as long as DNS for the subdomain points at this VPS:
#      yourdomain.example.com {
#          reverse_proxy instantly-domain-tracker:8000
#      }
#    then: docker exec caddy caddy reload --config /etc/caddy/Caddyfile

# 5. Schedule the daily ETL (7:00 AM IST) via systemd timer. Do NOT use
#    cron + CRON_TZ — Ubuntu's cron silently ignores CRON_TZ and runs the job
#    at 9 AM in the system timezone instead.
sudo cp deploy/domain-tracker-sync.service deploy/domain-tracker-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now domain-tracker-sync.timer
systemctl list-timers domain-tracker-sync.timer   # verify next run = 7:00 IST

# 6. Rotate the cron logs
sudo cp deploy/logrotate.conf /etc/logrotate.d/domain-tracker
```

7. **(Optional) GitHub Actions auto-deploy** — add a workflow that SSHs to the
   VM and runs `git pull && docker compose up -d --build`, and set repo secrets
   `VM_HOST`, `VM_USER`, `VM_SSH_KEY` (an SSH key already authorized on the VM).

`/health` is exempt from the password wall so uptime monitors can hit it without logging in.

## How It Works

1. **Step 1** — Fetch all accounts from `GET /api/v2/accounts` (paginated)
2. **Step 2** — Fetch campaign mappings per account (optional, can skip)
3. **Step 3** — Fetch daily analytics from `GET /api/v2/accounts/analytics/daily` (the goldmine — single call)
4. **Step 4** — Compute per-domain reply/bounce rates over the lookback window
5. **Step 5** — Classify and display in a color-coded CLI table

All API calls use `ON CONFLICT ... DO UPDATE` — the pipeline is idempotent and safe to re-run.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for local
setup, testing, and guidelines. For security issues, see
[SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE).

This project is an independent tool that integrates with the Instantly API. It
is not affiliated with or endorsed by Instantly.
