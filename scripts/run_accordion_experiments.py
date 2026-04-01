#!/usr/bin/env python3
"""Run accordion tuning experiments and capture comparable metrics.

This script applies `.env` settings for each experiment, restarts the
orchestrator container to reload env vars, executes runs against one fixed
manuscript, then prints/saves a metrics table.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ACCORDION_ACTIONS = {"lateral", "widen", "tighten", "accept", "exhausted"}
FINAL_STATUSES = {"complete", "partial", "failed"}
ACTIVE_STATUSES = {"queued", "analyzing", "planning", "pulling", "ingesting", "fitting"}


@dataclass(frozen=True)
class Experiment:
    label: str
    synonym_cap: int
    min_accept: int
    early_accept: int
    max_attempts: int = 4
    noise_threshold: int = 50

    def env_updates(self) -> Dict[str, str]:
        return {
            "ORCH_PULL_SYNONYM_CAP": str(self.synonym_cap),
            "ORCH_PULL_MIN_ACCEPT_DOCS": str(self.min_accept),
            "ORCH_PULL_EARLY_ACCEPT_DOCS": str(self.early_accept),
            "ORCH_PULL_MAX_QUERY_ATTEMPTS": str(self.max_attempts),
            "ORCH_PULL_NOISE_THRESHOLD": str(self.noise_threshold),
        }


def _json_request(url: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _write_env(path: Path, updates: Dict[str, str]) -> None:
    existing = _read_env(path)
    existing.update({k: str(v) for k, v in updates.items()})
    lines = [f"{k}={existing[k]}" for k in sorted(existing)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _docker_up(compose_path: Path) -> None:
    cmd = ["docker", "compose", "-f", str(compose_path), "up", "-d", "orchestrator"]
    subprocess.run(cmd, check=True)


def _docker_available() -> bool:
    """Return True if docker daemon is reachable."""

    result = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _start_local_server(repo_root: Path, port: int) -> subprocess.Popen:
    """Start local uvicorn process for experiment runs."""

    uvicorn_bin = repo_root / "app" / ".venv" / "bin" / "uvicorn"
    cmd = [str(uvicorn_bin), "app.main:app", "--host", "0.0.0.0", "--port", str(port)]
    return subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop_local_server(proc: subprocess.Popen | None) -> None:
    """Stop local uvicorn process if running."""

    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _wait_health(base_url: str, timeout_seconds: int = 120) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_err = ""
    while time.time() < deadline:
        try:
            data = _json_request(f"{base_url}/api/orchestrator/health")
            if data.get("status") == "ok":
                return data
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(2)
    raise RuntimeError(f"health check timeout: {last_err}")


def _wait_no_active_runs(base_url: str, timeout_seconds: int = 600) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        runs = _json_request(f"{base_url}/api/orchestrator/runs?limit=200").get("runs", [])
        active = [row for row in runs if str(row.get("status", "")) in ACTIVE_STATUSES]
        if not active:
            return
        time.sleep(2)
    raise RuntimeError("timed out waiting for active runs to clear")


def _start_run(base_url: str, manuscript_path: str, force: bool) -> str:
    payload = {"manuscript_path": manuscript_path, "force": force, "pull_timeout_seconds": 60}
    data = _json_request(f"{base_url}/api/orchestrator/runs", method="POST", payload=payload)
    run_id = str(data.get("run_id", "")).strip()
    if not run_id:
        raise RuntimeError(f"run creation failed: {data}")
    return run_id


def _wait_run(base_url: str, run_id: str, timeout_seconds: int = 1800) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last = {}
    while time.time() < deadline:
        row = _json_request(f"{base_url}/api/orchestrator/runs/{run_id}")
        status = str(row.get("status", ""))
        last = row
        if status in FINAL_STATUSES:
            return row
        time.sleep(3)
    raise RuntimeError(f"run timeout for {run_id}; last_status={last.get('status')}")


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _metrics_for_run(base_url: str, run: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(run.get("run_id", ""))
    events = _json_request(f"{base_url}/api/orchestrator/runs/{run_id}/events?limit=5000").get("events", [])
    pull_results = run.get("pull_results") or []
    plan = run.get("research_plan") or {}
    gaps = plan.get("gaps") or []

    total_docs = 0
    for gap in pull_results:
        for row in (gap.get("results") or []):
            try:
                total_docs += int(row.get("document_count", 0) or 0)
            except Exception:
                continue

    total_attempts = 0
    accept_rungs: Counter[str] = Counter()
    exhausted = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("stage", "")) != "pulling":
            continue
        status = str(event.get("status", ""))
        if status in ACCORDION_ACTIONS:
            total_attempts += 1
            if status == "accept":
                meta = event.get("meta") or {}
                rung = str(meta.get("rung", "")).strip() or "unknown"
                accept_rungs[rung] += 1
            if status == "exhausted":
                exhausted += 1

    conf = []
    needs_review = 0
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        if bool(gap.get("needs_review")):
            needs_review += 1
        val = gap.get("route_confidence")
        if val is not None:
            try:
                conf.append(float(val))
            except Exception:
                continue
    conf.sort()
    conf_p50 = conf[len(conf) // 2] if conf else 0.0
    conf_max = conf[-1] if conf else 0.0

    planning_start = None
    planning_end = None
    for event in events:
        if str(event.get("stage", "")) != "planning":
            continue
        ts = _parse_ts(str(event.get("ts_utc", "")))
        if ts is None:
            continue
        status = str(event.get("status", ""))
        if status == "started" and planning_start is None:
            planning_start = ts
        if status == "completed":
            planning_end = ts
    planning_seconds = None
    if planning_start and planning_end:
        planning_seconds = int((planning_end - planning_start).total_seconds())

    return {
        "run_id": run_id,
        "run_status": run.get("status", ""),
        "total_docs": total_docs,
        "total_attempts": total_attempts,
        "accept_rung_dist": dict(accept_rungs),
        "route_conf_p50": round(conf_p50, 3),
        "route_conf_max": round(conf_max, 3),
        "needs_review_count": needs_review,
        "exhausted_count": exhausted,
        "planning_seconds": planning_seconds,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base-url", default="http://127.0.0.1:8876")
    parser.add_argument("--restart-mode", choices=["auto", "docker", "local"], default="auto")
    parser.add_argument("--force", action="store_true", help="set run.force=true to bypass cached analysis")
    parser.add_argument(
        "--manuscript",
        default="Manuscript/Add To Cart -- main manuscript -- 2026.docx",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    env_path = repo_root / ".env"
    compose_path = repo_root / "app" / "docker-compose.yml"
    out_json = repo_root / "app" / "data" / "accordion_experiments_results.json"
    out_md = repo_root / "app" / "data" / "accordion_experiments_results.md"
    port = int(args.base_url.rsplit(":", 1)[-1])

    experiments = [
        Experiment("0a", synonym_cap=4, min_accept=2, early_accept=0),
        Experiment("0b", synonym_cap=4, min_accept=2, early_accept=0),
        Experiment("1", synonym_cap=4, min_accept=2, early_accept=0),
        Experiment("2", synonym_cap=2, min_accept=2, early_accept=0),
        Experiment("3a", synonym_cap=4, min_accept=2, early_accept=5),
        Experiment("3b", synonym_cap=4, min_accept=2, early_accept=10),
        Experiment("4", synonym_cap=4, min_accept=5, early_accept=0),
        # Start with the best candidate from runs above; can be re-run after inspection.
        Experiment("5a", synonym_cap=4, min_accept=2, early_accept=5),
        Experiment("5b", synonym_cap=4, min_accept=2, early_accept=5),
        Experiment("5c", synonym_cap=4, min_accept=2, early_accept=5),
    ]

    rows: List[Dict[str, Any]] = []
    current_env: Dict[str, str] = {}
    mode = args.restart_mode
    if mode == "auto":
        mode = "docker" if _docker_available() else "local"

    local_proc: subprocess.Popen | None = None
    try:
        for exp in experiments:
            updates = exp.env_updates()
            if updates != current_env:
                _write_env(env_path, updates)
                if mode == "docker":
                    _docker_up(compose_path)
                else:
                    _stop_local_server(local_proc)
                    local_proc = _start_local_server(repo_root, port)
                health = _wait_health(args.base_url)
                current_env = updates
                print(
                    f"[{exp.label}] service ready ({mode}): "
                    f"llm={health.get('llm_model')} "
                    f"playwright={len((health.get('availability') or {}).get('playwright_sources') or [])}"
                )

            _wait_no_active_runs(args.base_url)
            run_id = _start_run(args.base_url, args.manuscript, force=bool(args.force))
            print(f"[{exp.label}] started run: {run_id}")
            final = _wait_run(args.base_url, run_id)
            metrics = _metrics_for_run(args.base_url, final)
            row = {
                "exp": exp.label,
                "synonym_cap": exp.synonym_cap,
                "min_accept": exp.min_accept,
                "early_accept": exp.early_accept,
                **metrics,
            }
            rows.append(row)
            print(
                f"[{exp.label}] done status={row['run_status']} docs={row['total_docs']} "
                f"attempts={row['total_attempts']} conf_p50={row['route_conf_p50']}"
            )
    finally:
        if mode == "local":
            _stop_local_server(local_proc)

    out_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    lines = [
        "| Exp | Run ID | Synonym Cap | Min Accept | Early Accept | Total Docs | Total Attempts | Conf P50 | Conf Max | Needs Review | Exhausted |",
        "|-----|--------|-------------|------------|--------------|------------|----------------|----------|----------|--------------|-----------|",
    ]
    for row in rows:
        lines.append(
            "| {exp} | {run_id} | {synonym_cap} | {min_accept} | {early_accept} | "
            "{total_docs} | {total_attempts} | {route_conf_p50} | {route_conf_max} | "
            "{needs_review_count} | {exhausted_count} |".format(**row)
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nSaved:")
    print(f"  {out_json}")
    print(f"  {out_md}")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
