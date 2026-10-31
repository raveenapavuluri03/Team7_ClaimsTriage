"""Workflow state object, JSON persistence and step logging."""
import json
import os
import uuid
from datetime import datetime

STATE_DIR = "states"
LOG_PATH = os.path.join("logs", "run_log.jsonl")
STEP_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"]


def now():
    return datetime.now().isoformat(timespec="seconds")


def new_state(claim):
    """Create the state dictionary that is carried between all steps."""
    return {
        "run_id": uuid.uuid4().hex[:8],
        "claim_id": claim["claim_id"],
        "status": "RUNNING",          # see the transition table in the README / report (M3)
        "current_step": "S1",
        "status_reason": None,
        "input": claim,
        "config": {},
        "results": {},                # intermediate output of every step, keyed by step id
        "errors": [],                 # every error with step, attempt and message
        "retries": {},                # retry counters per step
        "human_actions": [],          # reviewer corrections / overrides / decisions
        "metrics": {"llm_calls": 0, "llm_latency_ms": 0, "prompt_tokens": 0, "output_tokens": 0},
        "started_at": now(),
        "updated_at": now(),
    }


def save_state(state):
    """Persist the full state after every step so a run can be resumed or audited."""
    os.makedirs(STATE_DIR, exist_ok=True)
    state["updated_at"] = now()
    to_save = {k: v for k, v in state.items() if not k.startswith("_")}   # skip in-memory-only items
    path = os.path.join(STATE_DIR, f"{state['claim_id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:       # write to a temp file, then swap in one step,
        json.dump(to_save, f, indent=2, default=str)   # so a crash never leaves a half-written state
    os.replace(tmp, path)


def load_state(claim_id):
    with open(os.path.join(STATE_DIR, f"{claim_id}.json"), encoding="utf-8") as f:
        return json.load(f)


def log_raw(claim_id, run_id, step, status, detail="", attempt=1, latency_ms=None):
    """Append one line per event to logs/run_log.jsonl and echo it to the console."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    rec = {"ts": now(), "run_id": run_id, "claim_id": claim_id, "step": step,
           "status": status, "attempt": attempt, "latency_ms": latency_ms, "detail": str(detail)[:300]}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"  [{step:>3}] {status:<9} attempt={attempt} {'' if latency_ms is None else str(latency_ms) + 'ms '}{rec['detail']}")


def log_step(state, step, status, detail="", attempt=1, latency_ms=None):
    log_raw(state["claim_id"], state["run_id"], step, status, detail, attempt, latency_ms)