#!/bin/bash
# Thin wrapper — delegates to the generic _yield_recovery.sh.
# Kept so any bookmarked invocations / cron entries / docs still work.
#
# Targets the 56 low-yield gaps (0–1 PDFs) identified before the 2026-05-01 run.
# Model: gpt-oss:20b (A/B experiment winner — +59% PDFs/seed vs llama3.1:8b).
#
# To run directly instead:
#   bash scripts/_yield_recovery.sh /tmp/low_yield_gaps.txt low_yield

exec bash "$(dirname "$0")/_yield_recovery.sh" /tmp/low_yield_gaps.txt low_yield gpt-oss:20b
