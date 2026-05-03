#!/bin/bash
# A/B experiment: normalize 5 low-yield gaps with each of two models,
# re-fetch, measure PDF uplift. One-off script — not for production.

set -u
cd "$(dirname "$0")/.."

ARM_A_GAPS=("AUTO-127-G1" "AUTO-136-G1" "AUTO-137-G1" "AUTO-138-G1" "AUTO-140-G1")
ARM_B_GAPS=("AUTO-141-G1" "AUTO-146-G1" "AUTO-148-G1" "AUTO-153-G1" "AUTO-157-G1")
ARM_A_MODEL="llama3.1:8b"
ARM_B_MODEL="gpt-oss:20b"

LOG="logs/ab_experiment.log"
echo "=== A/B EXPERIMENT START $(date '+%Y-%m-%d %H:%M:%S') ===" | tee "$LOG"

count_pdfs() {
    local g="$1"
    local d="data/pull_outputs/run_27f86e44394442/$g/ebsco_api/fetched"
    [ -d "$d" ] && find "$d" -name '*.pdf' 2>/dev/null | wc -l | tr -d ' ' || echo 0
}

# --- Capture pre-state ---
echo "" | tee -a "$LOG"
echo "--- pre-experiment PDF counts ---" | tee -a "$LOG"
for g in "${ARM_A_GAPS[@]}" "${ARM_B_GAPS[@]}"; do
    n=$(count_pdfs "$g")
    echo "  $g: $n PDFs" | tee -a "$LOG"
done

# --- Step 1: normalize Arm A with llama3.1:8b ---
echo "" | tee -a "$LOG"
echo "=== Step 1: normalize Arm A with $ARM_A_MODEL ($(date '+%H:%M:%S')) ===" | tee -a "$LOG"
A_NORM_START=$(date +%s)
for g in "${ARM_A_GAPS[@]}"; do
    echo "  normalizing $g..." | tee -a "$LOG"
    PYTHONUNBUFFERED=1 python3 -u scripts/normalize_seed_queries.py \
        --run-id run_27f86e44394442 \
        --gap-id "$g" \
        --variants 3 \
        --model "$ARM_A_MODEL" \
        --force \
        2>&1 | grep -v -i "deprecation\|trace-depr" | tail -5 | tee -a "$LOG"
done
A_NORM_ELAPSED=$(($(date +%s) - A_NORM_START))
echo "Arm A normalization elapsed: ${A_NORM_ELAPSED}s" | tee -a "$LOG"

# --- Step 2: normalize Arm B with gpt-oss:20b ---
echo "" | tee -a "$LOG"
echo "=== Step 2: normalize Arm B with $ARM_B_MODEL ($(date '+%H:%M:%S')) ===" | tee -a "$LOG"
B_NORM_START=$(date +%s)
for g in "${ARM_B_GAPS[@]}"; do
    echo "  normalizing $g..." | tee -a "$LOG"
    PYTHONUNBUFFERED=1 python3 -u scripts/normalize_seed_queries.py \
        --run-id run_27f86e44394442 \
        --gap-id "$g" \
        --variants 3 \
        --model "$ARM_B_MODEL" \
        --force \
        2>&1 | grep -v -i "deprecation\|trace-depr" | tail -5 | tee -a "$LOG"
done
B_NORM_ELAPSED=$(($(date +%s) - B_NORM_START))
echo "Arm B normalization elapsed: ${B_NORM_ELAPSED}s" | tee -a "$LOG"

# --- Step 3: fetch all 10 gaps with the multi-variant code ---
echo "" | tee -a "$LOG"
echo "=== Step 3: re-fetch all 10 gaps ($(date '+%H:%M:%S')) ===" | tee -a "$LOG"
FETCH_START=$(date +%s)
for g in "${ARM_A_GAPS[@]}" "${ARM_B_GAPS[@]}"; do
    echo "  fetching $g..." | tee -a "$LOG"
    ORCH_PDF_WORKERS=4 PYTHONUNBUFFERED=1 python3 -u scripts/fetch_documents.py \
        --run-id run_27f86e44394442 \
        --gap-id "$g" \
        --workers 4 \
        --ebsco-opid 6hfcoc --ebsco-db asn,bsu \
        --no-prompt --no-launch \
        2>&1 | grep -v -i "deprecation\|trace-depr" | grep -E "Articles extracted|Article PDFs|seed_failed|throttle" | tee -a "$LOG"
done
FETCH_ELAPSED=$(($(date +%s) - FETCH_START))
echo "Fetch elapsed: ${FETCH_ELAPSED}s" | tee -a "$LOG"

# --- Step 4: capture post-state and compute deltas ---
echo "" | tee -a "$LOG"
echo "=== RESULTS ===" | tee -a "$LOG"
echo "" | tee -a "$LOG"
printf "%-18s %-12s %-12s %-10s\n" "Gap" "PDFs before" "PDFs after" "Δ" | tee -a "$LOG"
echo "------------------ ------------ ------------ ----------" | tee -a "$LOG"

ARM_A_BEFORE_TOTAL=0; ARM_A_AFTER_TOTAL=0
ARM_B_BEFORE_TOTAL=0; ARM_B_AFTER_TOTAL=0

# Read baseline from /tmp/ab_state.json
BASELINE_FILE="/tmp/ab_state.json"

for g in "${ARM_A_GAPS[@]}"; do
    before=$(python3 -c "import json; d=json.load(open('$BASELINE_FILE'))['baseline_pdfs']; print(d['$g'])")
    after=$(count_pdfs "$g")
    delta=$((after - before))
    ARM_A_BEFORE_TOTAL=$((ARM_A_BEFORE_TOTAL + before))
    ARM_A_AFTER_TOTAL=$((ARM_A_AFTER_TOTAL + after))
    printf "%-18s %-12s %-12s %-10s\n" "$g (A)" "$before" "$after" "+$delta" | tee -a "$LOG"
done
for g in "${ARM_B_GAPS[@]}"; do
    before=$(python3 -c "import json; d=json.load(open('$BASELINE_FILE'))['baseline_pdfs']; print(d['$g'])")
    after=$(count_pdfs "$g")
    delta=$((after - before))
    ARM_B_BEFORE_TOTAL=$((ARM_B_BEFORE_TOTAL + before))
    ARM_B_AFTER_TOTAL=$((ARM_B_AFTER_TOTAL + after))
    printf "%-18s %-12s %-12s %-10s\n" "$g (B)" "$before" "$after" "+$delta" | tee -a "$LOG"
done

echo "" | tee -a "$LOG"
echo "=== SUMMARY ===" | tee -a "$LOG"
echo "Arm A ($ARM_A_MODEL):" | tee -a "$LOG"
echo "  PDFs: $ARM_A_BEFORE_TOTAL → $ARM_A_AFTER_TOTAL  (+$((ARM_A_AFTER_TOTAL - ARM_A_BEFORE_TOTAL)))" | tee -a "$LOG"
echo "  Normalize: ${A_NORM_ELAPSED}s" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Arm B ($ARM_B_MODEL):" | tee -a "$LOG"
echo "  PDFs: $ARM_B_BEFORE_TOTAL → $ARM_B_AFTER_TOTAL  (+$((ARM_B_AFTER_TOTAL - ARM_B_BEFORE_TOTAL)))" | tee -a "$LOG"
echo "  Normalize: ${B_NORM_ELAPSED}s" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Combined fetch (both arms): ${FETCH_ELAPSED}s" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "=== A/B EXPERIMENT END $(date '+%H:%M:%S') ===" | tee -a "$LOG"
