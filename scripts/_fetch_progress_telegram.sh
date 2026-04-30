#!/bin/bash
# Background ping script — sends a Telegram update every 30 min while
# the full fetch run is in progress. Exits when the python process dies.
#
# Reads pid from /tmp/fetch_run_pid and progress from logs/full_fetch_run.log.

set -u
LOG="logs/full_fetch_run.log"
PID_FILE="/tmp/fetch_run_pid"
START_FILE="/tmp/fetch_run_start"

send_telegram() {
    local msg="$1"
    python3 - <<PYEOF
import json, urllib.request, urllib.parse
from pathlib import Path
try:
    cfg = json.loads((Path.home() / ".claude/settings.json").read_text())
    env = cfg.get("env", {})
    token = env["TELEGRAM_BOT_TOKEN"]
    chat = env["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({"chat_id": str(chat), "text": ${msg@Q}}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print("telegram err:", e, flush=True)
PYEOF
}

# Wait until "Found .* items" appears (initial collection done)
while ! grep -q "Found .* items" "$LOG" 2>/dev/null; do
    sleep 5
done

# Initial summary
TOTAL_ITEMS=$(grep -oE "Found [0-9]+ items" "$LOG" | head -1 | grep -oE "[0-9]+")
send_telegram "[fetch_documents] Run started. ${TOTAL_ITEMS} seed pages queued."

# Periodic updates every 30 min
while kill -0 "$(cat "$PID_FILE")" 2>/dev/null; do
    sleep 1800
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        break
    fi
    NOW=$(date +%s)
    ELAPSED=$((NOW - $(cat "$START_FILE")))
    DONE=$(grep -c "fetching/seed_ok" "$LOG" 2>/dev/null || echo 0)
    BLOCKED=$(grep -c "fetching/blocked" "$LOG" 2>/dev/null || echo 0)
    PDFS=$(grep -c "pdf_inline_ok" "$LOG" 2>/dev/null || echo 0)
    NOPDF=$(grep -c "pdf_inline_unavailable" "$LOG" 2>/dev/null || echo 0)
    PCT=$((100 * DONE / (TOTAL_ITEMS > 0 ? TOTAL_ITEMS : 1)))
    send_telegram "[fetch_documents] Progress @ ${ELAPSED}s: ${DONE}/${TOTAL_ITEMS} pages (${PCT}%), ${PDFS} PDFs saved, ${NOPDF} no-PDF, ${BLOCKED} blocked."
done

# Final summary on completion
ELAPSED=$(($(date +%s) - $(cat "$START_FILE")))
DONE=$(grep -c "fetching/seed_ok" "$LOG" 2>/dev/null || echo 0)
PDFS=$(grep -c "pdf_inline_ok" "$LOG" 2>/dev/null || echo 0)
NOPDF=$(grep -c "pdf_inline_unavailable" "$LOG" 2>/dev/null || echo 0)
BLOCKED=$(grep -c "fetching/blocked" "$LOG" 2>/dev/null || echo 0)
FAILED=$(grep -c "seed_failed" "$LOG" 2>/dev/null || echo 0)
send_telegram "[fetch_documents] DONE in ${ELAPSED}s. Pages: ${DONE}/${TOTAL_ITEMS}. PDFs: ${PDFS} ok, ${NOPDF} unavailable, ${BLOCKED} blocked, ${FAILED} failed."
