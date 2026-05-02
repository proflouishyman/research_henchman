#!/bin/bash
# Two-way Telegram bridge — hourly status pinger + command dispatcher.
#
# Sends an immediate status ping on launch, then repeats every 3600 s.
# Between hourly ticks the loop wakes every ~60 s to poll getUpdates so
# user commands are answered within roughly one minute.
#
# Supported slash commands (case-insensitive):
#   /status  — send a fresh status ping immediately
#   /stop    — confirm + gracefully exit this script
#   /note    — append text to logs/telegram_user_notes.log
#   /runs    — show project PIDs + elapsed time
#   /disk    — corpus stats under data/pull_outputs/
#   /help    — list commands
#
# State file:  logs/telegram_poll_state.json  (tracks last_update_id)
# Notes file:  logs/telegram_user_notes.log
#
# Stop manually: kill $(cat /tmp/hourly_status_pid)

set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LOG="$ROOT/logs/hourly_status.log"
PID_FILE="/tmp/hourly_status_pid"
STOP_FLAG="/tmp/tg_bridge_stop_$$"

echo $$ > "$PID_FILE"
echo "=== telegram bridge started $(date) ===" >> "$LOG"

# ---------------------------------------------------------------------------
# Core Python helper — runs inline python3 for all Telegram I/O and commands.
# Receives two env-vars:
#   TG_ACTION  — one of: send_status | poll_and_dispatch
#   TG_ROOT    — absolute project root
# ---------------------------------------------------------------------------
run_python() {
    TG_ACTION="$1" TG_ROOT="$ROOT" TG_STOP_FLAG="$STOP_FLAG" \
    PYTHONUNBUFFERED=1 python3 - <<'PYEOF' 2>>"$LOG"
import json, os, subprocess, sys, time, urllib.request, urllib.parse
from pathlib import Path

ACTION    = os.environ["TG_ACTION"]     # "send_status" or "poll_and_dispatch"
ROOT      = Path(os.environ["TG_ROOT"])
STOP_FLAG = Path(os.environ["TG_STOP_FLAG"])

# ── credentials ──────────────────────────────────────────────────────────────
cfg   = json.loads((Path.home() / ".claude/settings.json").read_text())
env   = cfg["env"]
TOKEN = env["TELEGRAM_BOT_TOKEN"]
CHAT  = str(env["TELEGRAM_CHAT_ID"])

STATE_FILE = ROOT / "logs/telegram_poll_state.json"
NOTES_FILE = ROOT / "logs/telegram_user_notes.log"

# ── helpers ──────────────────────────────────────────────────────────────────

def send(text):
    """Send a plain-text message to the configured Telegram chat."""
    body = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body),
            timeout=10)
    except Exception as e:
        print(f"[tg send err] {e}", flush=True)


def running(pat):
    """Return True if any process matches the pgrep pattern."""
    r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
    return bool(r.stdout.strip())


def collect_status_lines():
    """Return the status message text (same content as the hourly ping)."""
    RUN  = "run_27f86e44394442"
    pull = ROOT / "data/pull_outputs" / RUN

    pdfs = sum(1 for _ in pull.rglob("*.pdf")) if pull.exists() else 0
    mds  = sum(1 for _ in pull.rglob("*.md"))  if pull.exists() else 0

    # Active project processes
    PATTERNS = {
        "_orchestrate_recovery": "orchestrator",
        "_yield_recovery.sh":    "recovery script",
        "normalize_seed_queries": "normalize",
        "fetch_documents.py":    "fetch",
        "pull_proquest_":        "proquest pull",
    }
    active = [label for pat, label in PATTERNS.items() if running(pat)]

    # Most-recently-updated relevant log
    candidates = [
        "logs/medium_yield_recovery.log",
        "logs/low_yield_recovery.log",
        "logs/full_fetch_run.log",
        "logs/orchestrate_recovery.log",
    ]
    log_path = None
    for c in candidates:
        p = ROOT / c
        if p.exists() and (log_path is None or p.stat().st_mtime > log_path.stat().st_mtime):
            log_path = p

    last_event = "(no log)"
    log_age_sec = -1
    if log_path:
        try:
            log_age_sec = int(time.time() - log_path.stat().st_mtime)
            for L in reversed(log_path.read_text().splitlines()[-50:]):
                if L.strip() and "Deprecation" not in L and "trace-deprecation" not in L:
                    last_event = L[:120]
                    break
        except Exception as e:
            last_event = f"(read err: {str(e)[:40]})"

    # Phase progress for medium-yield recovery
    phase_info = ""
    if active and log_path and log_path.name == "medium_yield_recovery.log":
        text = log_path.read_text()
        norm  = text.count("normalizing AUTO-")
        fetch = text.count("fetching AUTO-")
        if "Phase 2 elapsed" in text:
            phase_info = f"medium-yield COMPLETE (norm={norm}, fetch={fetch})"
        elif "Phase 1 elapsed" in text:
            phase_info = f"medium-yield Phase 2: fetch {fetch}"
        else:
            phase_info = f"medium-yield Phase 1: normalize {norm}"

    anomaly = ""
    if active and log_age_sec > 900:
        anomaly = f"\n⚠ log idle {log_age_sec // 60}m — possible stall"

    return (
        f"[hourly status @ {time.strftime('%H:%M')}]\n"
        f"PDFs: {pdfs}  MDs: {mds}\n"
        f"Active: {', '.join(active) if active else 'IDLE'}\n"
        + (f"Phase:  {phase_info}\n" if phase_info else "")
        + f"Last log ({log_path.name if log_path else 'none'}, {log_age_sec}s ago):\n"
        f"  {last_event}"
        + anomaly
    )


# ── command handlers ──────────────────────────────────────────────────────────

def cmd_status():
    send(collect_status_lines())


def cmd_stop():
    send("Stopping hourly Telegram bridge. Goodbye.")
    # Create stop flag — the bash loop checks for it each cycle
    STOP_FLAG.touch()
    print("[bridge] /stop received — stop flag written", flush=True)


def cmd_note(text):
    if not text.strip():
        send("Usage: /note <your text>")
        return
    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {text.strip()}"
    with NOTES_FILE.open("a") as f:
        f.write(line + "\n")
    send("Noted.")
    print(f"[bridge] note appended: {line[:80]}", flush=True)


def cmd_runs():
    """List running long-pipeline processes with PID + elapsed seconds."""
    SCAN_PATTERNS = [
        "_orchestrate_recovery",
        "_yield_recovery",
        "fetch_documents",
        "normalize_seed_queries",
        "pull_proquest_",
        "_hourly_status",
    ]
    lines = []
    now = time.time()
    for pat in SCAN_PATTERNS:
        r = subprocess.run(
            ["pgrep", "-f", pat], capture_output=True, text=True)
        for pid_str in r.stdout.strip().splitlines():
            pid = pid_str.strip()
            if not pid:
                continue
            # Get process start time via ps
            ps = subprocess.run(
                ["ps", "-p", pid, "-o", "lstart=,comm="],
                capture_output=True, text=True)
            if ps.returncode != 0:
                continue  # process gone
            info = ps.stdout.strip()
            lines.append(f"  PID {pid}: {info[:80]}")
    if lines:
        send("Running project processes:\n" + "\n".join(lines))
    else:
        send("No matching project processes found (IDLE).")


def cmd_disk():
    """Corpus stats: file counts and total bytes under data/pull_outputs/."""
    pull_root = ROOT / "data/pull_outputs"
    if not pull_root.exists():
        send("data/pull_outputs/ does not exist.")
        return
    pdf_count = md_count = json_count = 0
    total_bytes = 0
    for f in pull_root.rglob("*"):
        if not f.is_file():
            continue
        size = f.stat().st_size
        total_bytes += size
        sfx = f.suffix.lower()
        if sfx == ".pdf":
            pdf_count += 1
        elif sfx == ".md":
            md_count += 1
        elif sfx == ".json":
            json_count += 1
    mb = total_bytes / (1024 * 1024)
    send(
        f"Corpus stats (data/pull_outputs/):\n"
        f"  PDFs : {pdf_count}\n"
        f"  MDs  : {md_count}\n"
        f"  JSONs: {json_count}\n"
        f"  Total: {mb:.1f} MB"
    )


def cmd_help():
    send(
        "Available commands:\n"
        "  /status        — send a fresh status ping now\n"
        "  /stop          — gracefully stop this bridge\n"
        "  /note <text>   — append a timestamped note to logs/telegram_user_notes.log\n"
        "  /runs          — list running project processes with PID\n"
        "  /disk          — corpus file counts + total size under data/pull_outputs/\n"
        "  /help          — show this message"
    )


def dispatch(text):
    """Parse and execute one user command. Returns True if handled."""
    text = text.strip()
    lower = text.lower()
    if lower == "/status":
        cmd_status()
    elif lower == "/stop":
        cmd_stop()
    elif lower.startswith("/note"):
        # Everything after "/note" (with optional space) is the note body
        note_body = text[5:].lstrip()  # strip "/note"
        cmd_note(note_body)
    elif lower == "/runs":
        cmd_runs()
    elif lower == "/disk":
        cmd_disk()
    elif lower == "/help":
        cmd_help()
    elif lower.startswith("/"):
        send(f"Unknown command: {text.split()[0]}. Try /help.")
    else:
        # Not a slash command — ignore silently (could be a reply/conversation)
        pass


# ── poll helpers ─────────────────────────────────────────────────────────────

def load_last_update_id():
    try:
        return json.loads(STATE_FILE.read_text()).get("last_update_id", 0)
    except Exception:
        return 0


def save_last_update_id(uid):
    STATE_FILE.write_text(json.dumps({"last_update_id": uid}))


def poll_and_dispatch():
    """Fetch pending updates from Telegram, filter to our chat, dispatch commands."""
    last_id = load_last_update_id()
    url = (
        f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        f"?offset={last_id + 1}&limit=20&timeout=3"
    )
    try:
        raw  = urllib.request.urlopen(url, timeout=10).read()
        data = json.loads(raw)
    except Exception as e:
        print(f"[tg poll err] {e}", flush=True)
        return

    updates = data.get("result", [])
    for upd in updates:
        uid = upd["update_id"]
        # Always advance the cursor even if we skip the message
        if uid > last_id:
            last_id = uid

        msg = upd.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != CHAT:
            # Not our chat — skip (security: ignore third-party messages)
            continue

        text = msg.get("text", "")
        if text:
            print(f"[bridge] incoming: {text[:80]}", flush=True)
            dispatch(text)

    if updates:
        save_last_update_id(last_id)


# ── entry point ───────────────────────────────────────────────────────────────

if ACTION == "send_status":
    msg = collect_status_lines()
    send(msg)
    print(f"[{time.strftime('%H:%M:%S')}] status sent", flush=True)
elif ACTION == "poll_and_dispatch":
    poll_and_dispatch()
else:
    print(f"[bridge] unknown action: {ACTION}", flush=True)
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Main loop:
#   - Send status immediately on launch
#   - Every 60 s: poll for commands
#   - Every 3600 s: also send the hourly status ping
# ---------------------------------------------------------------------------

POLL_INTERVAL=60       # poll for commands every 60 seconds
HOURLY_INTERVAL=3600   # full status every hour
seconds_since_status=0

# Immediate ping
run_python send_status

while true; do
    sleep "$POLL_INTERVAL"

    # Check for /stop flag written by the Python handler
    if [ -f "$STOP_FLAG" ]; then
        echo "=== telegram bridge stopped by /stop command $(date) ===" >> "$LOG"
        rm -f "$STOP_FLAG" "$PID_FILE"
        exit 0
    fi

    # Poll for incoming commands
    run_python poll_and_dispatch

    # Check stop flag again immediately after dispatch
    if [ -f "$STOP_FLAG" ]; then
        echo "=== telegram bridge stopped by /stop command $(date) ===" >> "$LOG"
        rm -f "$STOP_FLAG" "$PID_FILE"
        exit 0
    fi

    seconds_since_status=$((seconds_since_status + POLL_INTERVAL))
    if [ "$seconds_since_status" -ge "$HOURLY_INTERVAL" ]; then
        run_python send_status
        seconds_since_status=0
    fi
done
