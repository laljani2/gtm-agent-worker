"""
Agent 01 worker — a thin always-on wrapper around agent01_ferc_scraper.py.

Endpoints:
  GET  /          health check
  POST /run       starts a run in a background thread (returns immediately)
  GET  /status    current run status (polled by the UI)

Both /run and /status are called server-to-server by the Vercel proxy
endpoints, which attach the shared X-Worker-Token header.
"""

import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import agent01_ferc_scraper as agent

app = Flask(__name__)

WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")

# Shared run state (single worker process, so an in-memory dict is fine)
_lock = threading.Lock()
STATUS = {
    "state":       "idle",   # idle | running | done | error
    "stage":       "",
    "message":     "Idle",
    "current":     0,
    "total":       0,
    "summary":     None,
    "started_at":  None,
    "finished_at": None,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _authorized():
    # If no token is configured, allow (useful for local testing).
    if not WORKER_TOKEN:
        return True
    return request.headers.get("X-Worker-Token", "") == WORKER_TOKEN


def _progress(update):
    """Called by the agent at each stage; merge into STATUS."""
    with _lock:
        STATUS["stage"]   = update.get("stage", STATUS["stage"])
        STATUS["message"] = update.get("message", STATUS["message"])
        STATUS["current"] = update.get("current", STATUS["current"])
        STATUS["total"]   = update.get("total", STATUS["total"])


def _run_job():
    try:
        summary = agent.run_agent(progress=_progress)
        with _lock:
            STATUS["state"]       = "done"
            STATUS["summary"]     = summary
            STATUS["finished_at"] = _now()
    except Exception as e:
        with _lock:
            STATUS["state"]       = "error"
            STATUS["message"]     = f"Run failed: {e}"
            STATUS["finished_at"] = _now()


@app.get("/")
def health():
    return "Agent 01 worker is running", 200


@app.post("/run")
def run():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        if STATUS["state"] == "running":
            return jsonify({"started": False, "reason": "already_running",
                            "status": STATUS}), 200
        # reset state for a fresh run
        STATUS.update({
            "state": "running", "stage": "starting", "message": "Starting run",
            "current": 0, "total": 0, "summary": None,
            "started_at": _now(), "finished_at": None,
        })

    threading.Thread(target=_run_job, daemon=True).start()
    return jsonify({"started": True, "status": STATUS}), 202


@app.get("/status")
def status():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    with _lock:
        return jsonify(STATUS), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)