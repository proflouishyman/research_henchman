"""Tests for the yield-recovery shell scripts.

Coverage:
  - _yield_recovery.sh passes bash -n (syntax check)
  - _orchestrate_recovery.sh passes bash -n (syntax check)
  - _low_yield_recovery.sh (thin wrapper) passes bash -n
  - _yield_recovery.sh exits 1 with a useful error when called with no args
  - _yield_recovery.sh exits 1 with a useful error when the gap file does not exist
  - _yield_recovery.sh prints the correct label into its startup banner when given
    a minimal fake gap file (mocking the python invocations so nothing runs for real)

Note: these tests do NOT run normalize_seed_queries.py or fetch_documents.py.
      They either use bash -n (parse-only) or intercept the first real python
      invocation by verifying that the script would reach it but stops before
      doing any network I/O.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
YIELD_RECOVERY   = SCRIPTS_DIR / "_yield_recovery.sh"
ORCHESTRATE      = SCRIPTS_DIR / "_orchestrate_recovery.sh"
LOW_YIELD_WRAPPER = SCRIPTS_DIR / "_low_yield_recovery.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bash_n(script: Path) -> subprocess.CompletedProcess:
    """Run bash -n on a script (syntax check, no execution)."""
    return subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Syntax checks (bash -n)
# ---------------------------------------------------------------------------

def test_yield_recovery_bash_syntax():
    """_yield_recovery.sh must parse without errors."""
    result = bash_n(YIELD_RECOVERY)
    assert result.returncode == 0, (
        f"bash -n failed on _yield_recovery.sh:\n{result.stderr}"
    )


def test_orchestrate_recovery_bash_syntax():
    """_orchestrate_recovery.sh must parse without errors."""
    result = bash_n(ORCHESTRATE)
    assert result.returncode == 0, (
        f"bash -n failed on _orchestrate_recovery.sh:\n{result.stderr}"
    )


def test_low_yield_wrapper_bash_syntax():
    """_low_yield_recovery.sh (thin wrapper) must parse without errors."""
    result = bash_n(LOW_YIELD_WRAPPER)
    assert result.returncode == 0, (
        f"bash -n failed on _low_yield_recovery.sh:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Argument / error-path tests  (actual execution, but exits fast)
# ---------------------------------------------------------------------------

def test_yield_recovery_no_args_exits_nonzero():
    """Script must exit non-zero and print usage when called with no arguments."""
    result = subprocess.run(
        ["bash", str(YIELD_RECOVERY)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Usage" in result.stderr or "Usage" in result.stdout


def test_yield_recovery_missing_gap_file_exits_nonzero():
    """Script must exit non-zero with an ERROR message when the gap file is absent."""
    result = subprocess.run(
        ["bash", str(YIELD_RECOVERY), "/tmp/__no_such_gaps_file__.txt", "test_label"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ERROR" in combined or "not found" in combined


def test_yield_recovery_banner_uses_label(tmp_path):
    """With a valid (1-entry) gap file the startup banner must echo the label.

    We intercept execution by giving the script a wrapper PATH that replaces
    python3 with a stub that immediately exits 0.  The bash `set -u` guard
    and the mapfile + echo block run before any python call, so we can safely
    observe the banner output without running any real normalize/fetch logic.

    The stub is placed first in PATH so `python3` resolves to it.
    """
    # Write a fake gap file with one gap ID
    gap_file = tmp_path / "test_gaps.txt"
    gap_file.write_text("AUTO-TEST-G1\n")

    # Stub that prints a harmless message and exits 0 immediately
    stub = tmp_path / "python3"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)

    # Also stub out find (used by count_pdfs_total) to avoid touching real data
    find_stub = tmp_path / "find"
    find_stub.write_text("#!/bin/bash\necho 0\n")
    find_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(YIELD_RECOVERY), str(gap_file), "MY_LABEL"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "HOME": str(Path.home()),
        },
    )
    # The script may succeed or fail (python stub exits 0 but the script
    # continues; outcome depends on set -u and the rest of the flow).
    # The key assertion is that the label appears in the combined output.
    combined = result.stdout + result.stderr
    assert "MY_LABEL" in combined, (
        f"Expected 'MY_LABEL' in output but got:\n{combined[:1000]}"
    )
    assert "1" in combined, "Expected gap count '1' in output"
