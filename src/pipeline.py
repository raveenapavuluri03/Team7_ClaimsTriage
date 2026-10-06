"""Workflow orchestrator: fixed step sequence with conditional branches, bounded retries,
fallbacks, state persistence, logging and human handoff."""
import json
import math
import os
import time

from src.data_prep import load_claims
from src.extraction import build_prompt, cross_check, parse_and_validate
from src.llm import llm_with_meta
from src.policies import check_coverage, retrieve_policy
from src.risk import load_model, ml_score, risk_band, rule_based_score, validate_score
from src.state import STEP_ORDER, load_state, log_step, new_state, now, save_state

# ---- Limits (referenced in the report) ----
MAX_API_ATTEMPTS = 3          # LLM call errors / timeouts: 3 attempts, backoff 1s, 2s
MAX_EXTRACTION_ATTEMPTS = 3   # invalid JSON / schema: first try + 2 re-prompts with the error message
AMOUNT_TOLERANCE = 0.05       # narrative vs form amount may differ by at most 5%
REQUIRED_FIELDS = ["claim_id", "policy_number", "form_incident_date", "form_claimed_amount", "narrative"]

_DF, _MODEL = None, None


def resources():
    global _DF, _MODEL
    if _DF is None:
        _DF = load_claims()
        _MODEL = load_model(_DF)
    return _DF, _MODEL


class LLMUnavailable(Exception):
    pass


def missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def call_llm_with_retry(state, step, prompt, json_mode=False):
    """Calls the model; on API errors retries up to MAX_API_ATTEMPTS with exponential backoff."""
    cfg = state["config"]
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            if cfg.get("force_api_fail"):
                raise ConnectionError("simulated LLM API outage")
            meta = llm_with_meta(prompt, model=cfg.get("model"), json_mode=json_mode)
            m = state["metrics"]
            m["llm_calls"] += 1
            m["llm_latency_ms"] += meta["latency_ms"]
            m["prompt_tokens"] += meta["prompt_tokens"]
            m["output_tokens"] += meta["output_tokens"]
            return meta
        except Exception as e:
            state["errors"].append({"step": step, "attempt": attempt, "type": "api_error", "error": str(e), "ts": now()})
            state["retries"][f"{step}_api"] = attempt
            log_step(state, step, "api_error", e, attempt)
            if attempt < MAX_API_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    raise LLMUnavailable(f"LLM unavailable after {MAX_API_ATTEMPTS} attempts")


# ------------------------------------------------------------------ steps
# Each step returns None to continue, or a terminal status string to stop the run.

def s1_validate_input(state):
    claim = state["input"]
    gaps = [f for f in REQUIRED_FIELDS if missing(claim.get(f))]
    state["results"]["S1"] = {"missing_fields": gaps}
    if gaps:
        state["status_reason"] = f"Missing required fields: {gaps}. Request them from the customer."
        log_step(state, "S1", "fail", state["status_reason"])
        return "PAUSED_MISSING_INFO"
    log_step(state, "S1", "ok", "all required fields present")


def s2_retrieve_policy(state):
    df, _ = resources()
    policy = retrieve_policy(df, state["input"]["policy_number"], query=state["input"]["narrative"])
    if policy is None:
        state["status_reason"] = "Policy number not found in the policy database."
        log_step(state, "S2", "fail", state["status_reason"])
        return "FAILED"
    state["results"]["S2"] = policy
    log_step(state, "S2", "ok", f"{policy['tier']}, limit ${policy['limit']:,}, deductible ${policy['deductible']:,.0f}")


def s3_extract(state):
    """S3 model call + S4 schema validation inside a bounded re-prompt loop."""
    cfg = state["config"]
    feedback, attempts = None, []
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        prompt = build_prompt(state["input"]["narrative"], cfg.get("prompt_version", "B"), feedback)
        try:
            if cfg.get("force_bad_json"):
                meta = {"text": "Sorry, I could not process this claim.", "latency_ms": 0}  # simulated bad output
            else:
                meta = call_llm_with_retry(state, "S3", prompt, json_mode=cfg.get("prompt_version", "B") == "B")
        except LLMUnavailable as e:
            state["status_reason"] = f"{e}. Routed to human exception review (H1)."
            log_step(state, "S3", "fail", state["status_reason"], attempt)
            return "ROUTED_TO_HUMAN"
        parsed, error = parse_and_validate(meta["text"])
        attempts.append({"attempt": attempt, "raw": meta["text"][:500], "valid": parsed is not None, "error": error})
        if parsed:
            state["results"]["S3"] = {"extracted": parsed, "attempts": attempts}
            log_step(state, "S3", "ok", parsed, attempt, meta["latency_ms"])
            return None
        state["retries"]["S3_reprompt"] = attempt
        state["errors"].append({"step": "S4", "attempt": attempt, "type": "invalid_output", "error": error, "ts": now()})
        log_step(state, "S4", "invalid", error, attempt, meta["latency_ms"])
        feedback = error
    state["results"]["S3"] = {"extracted": None, "attempts": attempts}
    state["status_reason"] = (f"Model output invalid after {MAX_EXTRACTION_ATTEMPTS} attempts. "
                              "Routed to human exception review (H1).")
    log_step(state, "S3", "fail", state["status_reason"], MAX_EXTRACTION_ATTEMPTS)
    return "ROUTED_TO_HUMAN"


def s4_cross_check(state):
    issues = cross_check(state["results"]["S3"]["extracted"], state["input"], AMOUNT_TOLERANCE)
    state["results"]["S4"] = {"issues": issues}
    if issues:
        state["status_reason"] = "Inconsistent submission: " + "; ".join(issues) + ". Routed to H1."
        log_step(state, "S4", "fail", state["status_reason"])
        return "ROUTED_TO_HUMAN"
    log_step(state, "S4", "ok", "narrative consistent with form")


def s5_coverage(state):
    ex, pol = state["results"]["S3"]["extracted"], state["results"]["S2"]
    cov = check_coverage(pol, state["input"]["form_incident_date"], ex["incident_type"],
                         float(state["input"]["form_claimed_amount"]), ex["police_report"])
    state["results"]["S5"] = cov
    if cov["result"] != "COVERED":
        state["status_reason"] = f"Coverage {cov['result']}: {' '.join(cov['reasons'])} Routed to H1."
        log_step(state, "S5", "fail", state["status_reason"])
        return "ROUTED_TO_HUMAN"
    log_step(state, "S5", "ok", f"COVERED, payout ${cov['payout']:,.0f}")


def s6_features(state):
    df, _ = resources()
    row = df[df["policy_number"] == int(state["input"]["policy_number"])].iloc[0].to_dict()
    ex = state["results"]["S3"]["extracted"]
    row.update({"incident_type": ex["incident_type"], "police_report_available": ex["police_report"],
                "total_claim_amount": float(state["input"]["form_claimed_amount"])})
    state["_record"] = row          # in-memory only (not saved; contains the full customer record)
    state["results"]["S6"] = {"features_built": True, "source": "policy/claims record + extracted fields"}
    log_step(state, "S6", "ok", "features built")


def s7_risk_score(state):
    _, model = resources()
    record = state.get("_record")
    rule_score, flags = rule_based_score(record)
    try:
        if state["config"].get("force_model_fail"):
            raise RuntimeError("simulated risk model failure")
        score, method = ml_score(model, record), "ml_logistic_regression"
    except Exception as e:
        state["errors"].append({"step": "S7", "attempt": 1, "type": "model_error", "error": str(e), "ts": now()})
        log_step(state, "S7", "fallback", f"{e}; using rule-based score")
        score, method = rule_score, "rule_based_fallback"
    state["results"]["S7"] = {"score": score, "method": method, "rule_score": rule_score, "flags": flags}
    log_step(state, "S7", "ok", f"score={score} ({method}), flags={flags}")


def s8_validate_risk(state):
    r = state["results"]["S7"]
    if not validate_score(r["score"]):
        log_step(state, "S8", "fallback", f"invalid score {r['score']}; using rule score")
        r["score"], r["method"] = r["rule_score"], "rule_based_fallback"
        if not validate_score(r["score"]):
            state["status_reason"] = "Risk score invalid after fallback. Routed to H1."
            return "ROUTED_TO_HUMAN"
    r["band"] = risk_band(r["score"])
    state["results"]["S8"] = {"valid": True, "band": r["band"]}
    log_step(state, "S8", "ok", f"risk band {r['band']}")


def s9_explain(state):
    ex, cov, risk = (state["results"]["S3"]["extracted"], state["results"]["S5"], state["results"]["S7"])
    prompt = (
        "You support an insurance claims adjuster. In 2-3 sentences, explain the fraud/risk indicators "
        "for this claim. Be factual, do not accuse the customer and do not recommend approval or denial.\n"
        f"Claim summary: {ex['summary']}\nIncident type: {ex['incident_type']}; police report: {ex['police_report']}\n"
        f"Coverage: {cov['result']} ({' '.join(cov['reasons'])})\n"
        f"Risk score: {risk['score']} ({risk['band']}, method {risk['method']}); rule flags: {risk['flags'] or 'none'}"
    )
    try:
        meta = call_llm_with_retry(state, "S9", prompt)
        text, source = meta["text"].strip(), "llm"
        if not text:
            raise ValueError("empty explanation")
    except Exception as e:
        text = (f"Risk score {risk['score']} ({risk['band']}). Indicators: "
                f"{', '.join(risk['flags']) if risk['flags'] else 'no rule-based flags triggered'}.")
        source = "template_fallback"
        log_step(state, "S9", "fallback", f"{e}; template explanation used")
    state["results"]["S9"] = {"explanation": text, "source": source}
    log_step(state, "S9", "ok", f"explanation from {source}")


def s10_package(state):
    res = state["results"]
    risk = res["S7"]
    action = ("Investigate before decision (high risk)" if risk["band"] == "HIGH"
              else "Review supporting documents" if risk["band"] == "MEDIUM" else "Standard review")
    package = {
        "claim_id": state["claim_id"],
        "policy": {k: res["S2"][k] for k in ["policy_number", "tier", "limit", "deductible", "bind_date"]},
        "relevant_policy_clauses": res["S2"]["relevant_clauses"],
        "extracted_claim": res["S3"]["extracted"],
        "form_claimed_amount": float(state["input"]["form_claimed_amount"]),
        "coverage": res["S5"],
        "risk": {k: risk[k] for k in ["score", "band", "method", "flags"]},
        "risk_explanation": res["S9"]["explanation"],
        "suggested_next_action": action,
        "decision": "PENDING - human adjuster must approve or deny",
    }
    os.makedirs("outputs", exist_ok=True)
    with open(os.path.join("outputs", f"{state['claim_id']}_package.json"), "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, default=str)
    res["S10"] = {"package_path": f"outputs/{state['claim_id']}_package.json", "suggested_next_action": action}
    state["status_reason"] = "Review package ready; waiting for adjuster decision (H2)."
    log_step(state, "S10", "ok", f"package saved, next action: {action}")
    return "COMPLETED_PENDING_ADJUSTER"


STEPS = {"S1": s1_validate_input, "S2": s2_retrieve_policy, "S3": s3_extract, "S4": s4_cross_check,
         "S5": s5_coverage, "S6": s6_features, "S7": s7_risk_score, "S8": s8_validate_risk,
         "S9": s9_explain, "S10": s10_package}


# ------------------------------------------------------------------ orchestration

def run_from(state, start_step):
    """Runs the fixed sequence from start_step until a step returns a terminal status."""
    t0 = time.time()
    state["status"] = "RUNNING"
    for step in STEP_ORDER[STEP_ORDER.index(start_step):]:
        state["current_step"] = step
        save_state(state)                       # checkpoint before each step (supports resume)
        if step in ("S7", "S8", "S9", "S10") and "_record" not in state:
            s6_features(state)                  # rebuild in-memory features after a resume
        try:
            outcome = STEPS[step](state)
        except Exception as e:                  # unexpected crash -> FAILED, state kept for resume
            state["errors"].append({"step": step, "attempt": 1, "type": "crash", "error": repr(e), "ts": now()})
            state["status_reason"] = f"Unexpected error in {step}: {e}"
            log_step(state, step, "crash", e)
            outcome = "FAILED"
        if outcome:
            state["status"] = outcome
            break
    state["metrics"]["pipeline_ms"] = state["metrics"].get("pipeline_ms", 0) + int((time.time() - t0) * 1000)
    state.pop("_record", None)
    save_state(state)
    log_step(state, "END", state["status"], state["status_reason"])
    return state


def run_claim(claim, prompt_version="B", model=None, force_bad_json=False, force_api_fail=False,
              force_model_fail=False):
    """Entry point (trigger): one submitted claim -> one workflow run."""
    claim = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in claim.items()}
    state = new_state(claim)
    state["config"] = {"prompt_version": prompt_version, "model": model, "force_bad_json": force_bad_json,
                       "force_api_fail": force_api_fail, "force_model_fail": force_model_fail}
    print(f"\n=== Claim {state['claim_id']} (run {state['run_id']}) ===")
    return run_from(state, "S1")


def resume_claim(claim_id, reviewer, note="", corrected_fields=None, new_input=None,
                 override_coverage=False, clear_test_flags=True):
    """Human exception review (H1) then resume from the right step using the saved state."""
    state = load_state(claim_id)
    if clear_test_flags:
        for k in ("force_bad_json", "force_api_fail", "force_model_fail"):
            state["config"][k] = False
    action = {"ts": now(), "reviewer": reviewer, "note": note, "previous_status": state["status"]}
    print(f"\n=== Resume {claim_id} by {reviewer} (was {state['status']} at {state['current_step']}) ===")
    if new_input:                                   # customer supplied missing info -> restart at S1
        state["input"].update(new_input)
        action["action"], start = f"added missing fields {list(new_input)}", "S1"
    elif corrected_fields:                          # reviewer corrected extraction -> continue at S5
        base = (state["results"].get("S3") or {}).get("extracted") or {}
        base.update(corrected_fields)
        state["results"]["S3"] = {"extracted": base, "attempts": [], "corrected_by": reviewer}
        state["results"]["S4"] = {"issues": [], "overridden_by": reviewer}
        action["action"], start = f"corrected fields {list(corrected_fields)}", "S5"
    elif override_coverage:                         # reviewer confirms coverage -> continue at S6
        state["results"]["S5"]["result"] = "COVERED"
        state["results"]["S5"]["reasons"].append(f"Coverage confirmed by reviewer {reviewer}.")
        action["action"], start = "coverage confirmed", "S6"
    else:                                           # plain restart of the step that stopped
        action["action"], start = "retry from last step", state["current_step"]
    state["human_actions"].append(action)
    log_step(state, "H1", "human", f"{reviewer}: {action['action']} - {note}")
    return run_from(state, start)


def adjuster_decision(claim_id, decision, reviewer, note=""):
    """H2 (simulated): final human decision. Payment / notification systems are mocked."""
    state = load_state(claim_id)
    if state["status"] != "COMPLETED_PENDING_ADJUSTER":
        raise ValueError(f"{claim_id} is {state['status']}; only completed packages can be decided.")
    decision = decision.upper()
    assert decision in ("APPROVE", "DENY")
    state["status"] = "APPROVED" if decision == "APPROVE" else "DENIED"
    state["human_actions"].append({"ts": now(), "reviewer": reviewer, "action": decision, "note": note})
    log_step(state, "H2", "human", f"{reviewer}: {decision} - {note}")
    if decision == "APPROVE":
        log_step(state, "EXT", "mock", f"Payment system (simulated): payout ${state['results']['S5']['payout']:,.0f} queued")
    log_step(state, "EXT", "mock", f"Notification service (simulated): customer informed of {state['status']}")
    save_state(state)
    return state