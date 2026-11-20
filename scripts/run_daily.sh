#!/bin/bash
# Instantly Domain Burn Detector — Daily ETL Script
# The app runs in a Docker container (see docker-compose.yml); this script
# runs on the HOST via cron and execs the CLI inside that running container.
# Splits stdout -> data/cron.log and stderr -> data/cron.err. On failure,
# calls notify_failure() below.
#
# Runs at 7:00 AM IST daily via a systemd timer (see deploy/domain-tracker-sync.timer).
# NOTE: do NOT schedule this with cron + CRON_TZ — Ubuntu's cron silently
# ignores CRON_TZ and runs the job at 9 AM in the system timezone instead.
CONTAINER=instantly-domain-tracker

cd "$(dirname "$0")/.." || exit 1
source .env 2>/dev/null   # only needed here for ALERT_WEBHOOK_URL

mkdir -p data
: > data/cron.err   # truncate — only this run's errors, not an ever-growing file

docker exec -w /app "$CONTAINER" python -m domain_tracker run \
    --skip-campaigns --skip-campaign-sync --no-report \
    >> data/cron.log 2>> data/cron.err
STATUS=$?

notify_failure() {
    local message="$1"
    # ALERT_WEBHOOK_URL (set in .env) — POSTs a JSON payload to Slack/Discord/
    # any generic webhook. No-op if unset.
    if [ -n "$ALERT_WEBHOOK_URL" ]; then
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"text\": \"$message\"}" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1
    fi
}

if [ $STATUS -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] run_daily.sh FAILED (exit $STATUS)" >> data/cron.err
    notify_failure "🔥 instantly-domain-tracker: daily sync FAILED (exit $STATUS) — check data/cron.err"
elif [ -s data/cron.err ]; then
    # Exit 0 but something was written to stderr — worth a look, not a hard failure.
    notify_failure "⚠️ instantly-domain-tracker: daily sync finished with warnings — check data/cron.err"
fi

exit $STATUS
