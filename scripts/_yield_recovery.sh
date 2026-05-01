#!/bin/bash
# Generic yield-recovery script: normalize + re-fetch a list of gap IDs.
#
# Usage:
#   scripts/_yield_recovery.sh <gap_list_file> <run_label> [normalize_model]
#
# Arguments:
#   <gap_list_file>    — path to a newline-separated file of gap IDs
#   <run_label>        — short string used in log filenames and Telegram pings
#                        (e.g. "low_yield", "medium_yield", "high_yield")
#   [normalize_model]  — optional; defaults to gpt-oss:20b (A/B experiment winner)
#
# Phase 1: normalize each gap's seed records via multi-variant query normalization
# Phase 2: re-fetch each gap with broader EBSCO coverage
# Phase 3: re-index the article index (--dedupe) so results are searchable immediately
#
# Telegram pings at phase boundaries and every 15 gaps during fetch.
#
# The script is hardcoded to run_27f86e44394442 — all recovery passes in this
# project target that run.  If a second run ever needs recovery, promote RUN_ID
# to a positional arg.

set -u
cd "$(dirname "$0")/.."

# ── Argument handling ─────────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    echo "Usage: $0 <gap_list_file> <run_label> [normalize_model]" >&2
    exit 1
fi

GAPS_FILE="$1"
LABEL="$2"
NORMALIZE_MODEL="${3:-gpt-oss:20b}"
RUN_ID="run_27f86e44394442"

if [ ! -f "$GAPS_FILE" ]; then
    echo "ERROR: gap list file not found: $GAPS_FILE" >&2
    exit 1
fi

LOG="logs/${LABEL}_recovery.log"

# ── Telegram helper ───────────────────────────────────────────────────────────
send_telegram() {
    local msg="$1"
    python3 - <<PYEOF 2>/dev/null
import json, urllib.request, urllib.parse
from pathlib import Path
try:
    cfg = json.loads((Path.home() / ".claude/settings.json").read_text())
    env = cfg["env"]
    body = urllib.parse.urlencode({"chat_id": str(env["TELEGRAM_CHAT_ID"]), "text": "$msg"}).encode()
    urllib.request.urlopen(
        urllib.request.Request(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", data=body),
        timeout=10)
except Exception:
    pass
PYEOF
}

# ── PDF counter ───────────────────────────────────────────────────────────────
count_pdfs_total() {
    find data/pull_outputs/${RUN_ID} -name '*.pdf' 2>/dev/null | wc -l | tr -d ' '
}

# ── Load gap list ──────────────────────────────────────────────────────────────
mapfile -t GAPS < "$GAPS_FILE"
N_GAPS=${#GAPS[@]}

echo "=== ${LABEL} recovery start $(date '+%Y-%m-%d %H:%M:%S') ===" | tee "$LOG"
echo "Gaps file : $GAPS_FILE"                                         | tee -a "$LOG"
echo "Gaps      : $N_GAPS"                                            | tee -a "$LOG"
echo "Label     : $LABEL"                                             | tee -a "$LOG"
echo "Model     : $NORMALIZE_MODEL"                                   | tee -a "$LOG"
echo "Run ID    : $RUN_ID"                                            | tee -a "$LOG"
echo ""                                                               | tee -a "$LOG"

PRE_PDF_TOTAL=$(count_pdfs_total)
START_EPOCH=$(date +%s)
echo "Starting PDFs on disk: $PRE_PDF_TOTAL" | tee -a "$LOG"

send_telegram "[${LABEL}_recovery] Phase 1 (normalize) starting on $N_GAPS gaps with $NORMALIZE_MODEL."

# ── Phase 1: normalize ────────────────────────────────────────────────────────
echo ""                                                                | tee -a "$LOG"
echo "=== Phase 1: normalize ($(date '+%H:%M:%S')) ==="               | tee -a "$LOG"
NORM_START=$(date +%s)
i=0
for g in "${GAPS[@]}"; do
    i=$((i+1))
    echo "[$i/$N_GAPS] normalizing $g" | tee -a "$LOG"
    PYTHONUNBUFFERED=1 python3 -u scripts/normalize_seed_queries.py \
        --run-id "$RUN_ID" \
        --gap-id "$g" \
        --variants 3 \
        --model "$NORMALIZE_MODEL" \
        --force 2>&1 \
        | grep -v -i "deprecation\|trace-depr" \
        | tail -3 | tee -a "$LOG"
    # Telegram ping every 15 gaps
    if [ $((i % 15)) -eq 0 ]; then
        send_telegram "[${LABEL}_recovery] Phase 1 progress: $i/$N_GAPS gaps normalized."
    fi
done
NORM_ELAPSED=$(($(date +%s) - NORM_START))
echo "Phase 1 elapsed: ${NORM_ELAPSED}s ($((NORM_ELAPSED/60))m)" | tee -a "$LOG"
send_telegram "[${LABEL}_recovery] Phase 1 done in ${NORM_ELAPSED}s. Phase 2 (fetch) starting now."

# ── Phase 2: fetch ────────────────────────────────────────────────────────────
echo ""                                                                | tee -a "$LOG"
echo "=== Phase 2: fetch ($(date '+%H:%M:%S')) ==="                   | tee -a "$LOG"
FETCH_START=$(date +%s)
i=0
for g in "${GAPS[@]}"; do
    i=$((i+1))
    echo "[$i/$N_GAPS] fetching $g" | tee -a "$LOG"
    ORCH_PDF_WORKERS=4 PYTHONUNBUFFERED=1 python3 -u scripts/fetch_documents.py \
        --run-id "$RUN_ID" \
        --gap-id "$g" \
        --workers 4 \
        --ebsco-opid 6hfcoc --ebsco-db asn,bsu \
        --no-prompt --no-launch 2>&1 \
        | grep -v -i "deprecation\|trace-depr" \
        | grep -E "Articles extracted|Article PDFs|seed_failed|throttle" \
        | tee -a "$LOG"
    # Telegram ping every 15 gaps with running delta
    if [ $((i % 15)) -eq 0 ]; then
        cur_total=$(count_pdfs_total)
        delta=$((cur_total - PRE_PDF_TOTAL))
        send_telegram "[${LABEL}_recovery] Phase 2 progress: $i/$N_GAPS gaps fetched. New PDFs so far: $delta"
    fi
done
FETCH_ELAPSED=$(($(date +%s) - FETCH_START))
echo "Phase 2 elapsed: ${FETCH_ELAPSED}s ($((FETCH_ELAPSED/60))m)" | tee -a "$LOG"
send_telegram "[${LABEL}_recovery] Phase 2 done in ${FETCH_ELAPSED}s. Phase 3 (re-index) starting now."

# ── Phase 3: re-index ─────────────────────────────────────────────────────────
# Updates the article index so medium-yield results are searchable immediately.
# --dedupe resolves DOI duplicates introduced by multi-variant fetch.
echo ""                                                                | tee -a "$LOG"
echo "=== Phase 3: re-index ($(date '+%H:%M:%S')) ==="               | tee -a "$LOG"
INDEX_START=$(date +%s)
python3 scripts/index_articles.py --run-id "$RUN_ID" --dedupe 2>&1 | tee -a "$LOG"
INDEX_EXIT=$?
INDEX_ELAPSED=$(($(date +%s) - INDEX_START))
if [ $INDEX_EXIT -ne 0 ]; then
    echo "WARNING: index step exited with code $INDEX_EXIT — continuing to summary" | tee -a "$LOG"
    send_telegram "[${LABEL}_recovery] WARNING: re-index step exited $INDEX_EXIT. Check $LOG."
else
    echo "Phase 3 elapsed: ${INDEX_ELAPSED}s" | tee -a "$LOG"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(($(date +%s) - START_EPOCH))
POST_PDF_TOTAL=$(count_pdfs_total)
DELTA=$((POST_PDF_TOTAL - PRE_PDF_TOTAL))
echo ""                                                                     | tee -a "$LOG"
echo "=== SUMMARY ==="                                                      | tee -a "$LOG"
echo "Total elapsed   : ${TOTAL_ELAPSED}s ($((TOTAL_ELAPSED/60))m)"        | tee -a "$LOG"
echo "Phase 1 (norm)  : ${NORM_ELAPSED}s"                                  | tee -a "$LOG"
echo "Phase 2 (fetch) : ${FETCH_ELAPSED}s"                                 | tee -a "$LOG"
echo "Phase 3 (index) : ${INDEX_ELAPSED}s"                                 | tee -a "$LOG"
echo "PDFs before     : $PRE_PDF_TOTAL"                                     | tee -a "$LOG"
echo "PDFs after      : $POST_PDF_TOTAL"                                    | tee -a "$LOG"
echo "Delta           : +$DELTA"                                            | tee -a "$LOG"
echo ""                                                                     | tee -a "$LOG"
echo "=== Done $(date '+%H:%M:%S') ==="                                    | tee -a "$LOG"

send_telegram "[${LABEL}_recovery] DONE in ${TOTAL_ELAPSED}s. PDFs: $PRE_PDF_TOTAL → $POST_PDF_TOTAL (+$DELTA). Ph1: ${NORM_ELAPSED}s Ph2: ${FETCH_ELAPSED}s Ph3: ${INDEX_ELAPSED}s."
