#!/bin/bash
# Dispatcher: waits for the low-yield recovery to finish, then runs medium-yield
# (and optionally high-yield) recovery passes in sequence.
#
# Usage (background-friendly):
#   nohup bash scripts/_orchestrate_recovery.sh >> logs/orchestrate_recovery.log 2>&1 &
#
# The script polls /tmp/low_yield_recovery_pid every 60 s to detect when the
# currently-running low-yield pass (PID 76163) exits.  It then:
#   1. Re-indexes the article index (--dedupe) to capture any new low-yield PDFs.
#   2. Snapshots medium-yield gaps FRESH (exactly 2 PDFs on disk at that moment).
#      NOTE: the gap list snapshotted here may differ from /tmp/medium_yield_gaps.txt
#      written earlier — the low-yield run is adding PDFs to the run directory
#      throughout.  Always re-snapshot just before dispatch.
#   3. Sends a Telegram ping and kicks off the medium-yield recovery.
#   (Optional) Repeats once more for high-yield gaps (3–5 PDFs).

set -u
cd "$(dirname "$0")/.."

LOG="logs/orchestrate_recovery.log"
PID_FILE="/tmp/low_yield_recovery_pid"
RUN_ID="run_27f86e44394442"
POLL_INTERVAL=60   # seconds between liveness checks

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

# ── Helper: snapshot gaps with exactly N PDFs ─────────────────────────────────
# Writes one gap_id per line to $2; echoes count to stdout.
# CAUTION: the run directory is live while the low-yield process is running —
# any snapshot taken before low-yield completes will be stale.  Always call
# this function AFTER confirming the prior phase is done.
snapshot_gaps_by_pdf_count() {
    local min_pdfs="$1"
    local max_pdfs="$2"
    local outfile="$3"
    python3 - <<PYEOF
from pathlib import Path
run_dir = Path("data/pull_outputs/${RUN_ID}")
gaps = []
for gap_dir in sorted(run_dir.iterdir()):
    if not gap_dir.is_dir():
        continue
    count = sum(
        len(list((src / "fetched").glob("*.pdf")))
        for src in gap_dir.iterdir()
        if src.is_dir() and (src / "fetched").is_dir()
    )
    if ${min_pdfs} <= count <= ${max_pdfs}:
        gaps.append(gap_dir.name)
with open("${outfile}", "w") as f:
    f.write("\n".join(gaps) + ("\n" if gaps else ""))
print(len(gaps))
PYEOF
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo "=== orchestrate_recovery start $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"
echo "Waiting for low-yield recovery (PID file: $PID_FILE)" | tee -a "$LOG"

# ── Step 1: poll for low-yield PID to exit ────────────────────────────────────
# Handles two cases:
#   a) PID file exists and contains a valid PID   → poll kill -0
#   b) PID file does not exist or is stale        → assume already done
#      (stale = file exists but PID is not alive — we proceed immediately)
if [ -f "$PID_FILE" ]; then
    LOW_PID=$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$LOW_PID" ]; then
        echo "PID file is empty — treating low-yield as already done." | tee -a "$LOG"
    else
        echo "Detected low-yield PID: $LOW_PID — polling every ${POLL_INTERVAL}s" | tee -a "$LOG"
        while kill -0 "$LOW_PID" 2>/dev/null; do
            sleep "$POLL_INTERVAL"
        done
        echo "Low-yield PID $LOW_PID exited at $(date '+%H:%M:%S')" | tee -a "$LOG"
    fi
else
    echo "No PID file at $PID_FILE — assuming low-yield already complete." | tee -a "$LOG"
fi

# ── Step 2: re-index after low-yield ─────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "=== Post-low-yield re-index ($(date '+%H:%M:%S')) ===" | tee -a "$LOG"
python3 scripts/index_articles.py --run-id "$RUN_ID" --dedupe 2>&1 | tee -a "$LOG"
INDEX_EXIT=$?
if [ $INDEX_EXIT -ne 0 ]; then
    # Non-zero exit from the indexer is non-fatal: the index may simply not yet
    # exist if no articles have been fetched.  Log and continue.
    echo "WARNING: index step exited $INDEX_EXIT — proceeding to medium-yield dispatch." | tee -a "$LOG"
fi

# ── Step 3: snapshot medium-yield gaps FRESH ──────────────────────────────────
# Re-snapshot NOW (after low-yield has stopped writing) so we work with the
# final post-low-yield PDF counts, not the snapshot taken hours earlier.
echo "" | tee -a "$LOG"
echo "=== Snapshotting medium-yield gaps ($(date '+%H:%M:%S')) ===" | tee -a "$LOG"
MED_GAPS_FILE="/tmp/medium_yield_gaps.txt"
MED_N=$(snapshot_gaps_by_pdf_count 2 2 "$MED_GAPS_FILE")
echo "Medium-yield gaps (exactly 2 PDFs): $MED_N — written to $MED_GAPS_FILE" | tee -a "$LOG"

# ── Step 4: Telegram ping + launch medium-yield ────────────────────────────────
send_telegram "[orchestrate_recovery] low-yield done, medium-yield starting on $MED_N gaps."
echo "" | tee -a "$LOG"
echo "=== Launching medium-yield recovery on $MED_N gaps ===" | tee -a "$LOG"
bash scripts/_yield_recovery.sh "$MED_GAPS_FILE" medium_yield 2>&1 | tee -a "$LOG"

# ── Step 5 (optional): high-yield gaps (3–5 PDFs) ────────────────────────────
# Uncomment if you want a third pass.  Kept in <10 lines as requested.
#
# echo "=== Snapshotting high-yield gaps ===" | tee -a "$LOG"
# HI_GAPS_FILE="/tmp/high_yield_gaps.txt"
# HI_N=$(snapshot_gaps_by_pdf_count 3 5 "$HI_GAPS_FILE")
# echo "High-yield gaps (3-5 PDFs): $HI_N" | tee -a "$LOG"
# send_telegram "[orchestrate_recovery] medium-yield done, high-yield starting on $HI_N gaps."
# bash scripts/_yield_recovery.sh "$HI_GAPS_FILE" high_yield 2>&1 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== orchestrate_recovery DONE $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"
send_telegram "[orchestrate_recovery] All recovery phases complete. Check $LOG for full summary."
