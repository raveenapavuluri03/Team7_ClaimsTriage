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
        "status": "RUNNING",          # RUNNING | PAUSED_MISSING_INFO | ROUTED_TO_HUMAN | FAILED
                                      # | COMPLETED_PENDING_ADJUSTER | APPROVED | DENIED
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
    with open(os.path.join(STATE_DIR, f"{state['claim_id']}.json"), "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2, default=str)


def load_state(claim_id):
    with open(os.path.join(STATE_DIR, f"{claim_id}.json"), encoding="utf-8") as f:
        return json.load(f)


def log_step(state, step, status, detail="", attempt=1, latency_ms=None):
    """Append one line per event to logs/run_log.jsonl and echo it to the console."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    rec = {"ts": now(), "run_id": state["run_id"], "claim_id": state["claim_id"], "step": step,
           "status": status, "attempt": attempt, "latency_ms": latency_ms, "detail": str(detail)[:300]}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"  [{step:>3}] {status:<9} attempt={attempt} {'' if latency_ms is None else str(latency_ms) + 'ms '}{rec['detail']}")