"""Runs every test claim through the workflow, then demonstrates human review (H1), resume,
an LLM outage, a risk-model failure and the final adjuster decision (H2).
Writes results/test_results.csv and prints a metrics summary."""
import os
import pandas as pd
from src.pipeline import adjuster_decision, resume_claim, run_claim

claims = pd.read_csv("data/test_claims.csv")
os.makedirs("results", exist_ok=True)
rows = []


def strip(row):
    """Keep only what a real submission contains (drop the answer key)."""
    return {k: v for k, v in row.items()
            if not k.startswith("true_") and k not in ("expected_status", "case_type", "fraud_reported", "force_bad_json")}


def record(test, case, expected, state, note="", passed=None):
    m = state["metrics"]
    rows.append({"test": test, "claim_id": state["claim_id"], "case": case, "expected": expected,
                 "actual": state["status"], "pass": (expected == state["status"]) if passed is None else passed,
                 "stopped_at": state["current_step"], "reason": state["status_reason"],
                 "retries": state["retries"], "llm_calls": m["llm_calls"],
                 "llm_latency_s": round(m["llm_latency_ms"] / 1000, 1),
                 "pipeline_s": round(m.get("pipeline_ms", 0) / 1000, 1),
                 "output_tokens": m["output_tokens"], "note": note})


# ---- Part 1: all 12 test claims ----
for _, r in claims.iterrows():
    row = r.to_dict()
    state = run_claim(strip(row), force_bad_json=bool(row["force_bad_json"]))
    record("main", row["case_type"], row["expected_status"], state)

# ---- Part 2: extra failure paths ----
base = strip(claims[claims["claim_id"] == "TC1"].iloc[0].to_dict())
state = run_claim({**base, "claim_id": "TC1-API-OUTAGE"}, force_api_fail=True)
record("failure", "LLM API outage (simulated)", "ROUTED_TO_HUMAN", state)

state = run_claim({**base, "claim_id": "TC1-MODEL-FAIL"}, force_model_fail=True)
record("failure", "Risk model error -> rule fallback", "COMPLETED_PENDING_ADJUSTER", state,
       note=f"risk method used: {state['results'].get('S7', {}).get('method')}")

# ---- Part 3: human review (H1) and resume from saved state ----
tc2 = claims[claims["claim_id"] == "TC2"].iloc[0]
state = resume_claim("TC2", reviewer="claims_clerk", note="customer supplied the claimed amount",
                     new_input={"form_claimed_amount": float(tc2["true_amount"])})
record("resume", "TC2 after customer adds missing amount", "COMPLETED_PENDING_ADJUSTER", state)

state = resume_claim("TC3", reviewer="adjuster_1", note="police report received by email",
                     override_coverage=True)
record("resume", "TC3 after reviewer confirms coverage", "COMPLETED_PENDING_ADJUSTER", state)

state = resume_claim("TC4", reviewer="adjuster_1", note="model output issue fixed; re-run extraction")
record("resume", "TC4 re-run after invalid output", "extraction succeeds (passes S3)", state,
       note="after S3 the claim follows normal rules, so it may still go to H1 at S5",
       passed=state["current_step"] not in ("S3", "S4") and state["status"] != "FAILED")

tc5 = claims[claims["claim_id"] == "TC5"].iloc[0]
state = resume_claim("TC5", reviewer="adjuster_1", note="invoice confirms form amount",
                     corrected_fields={"amount": float(tc5["form_claimed_amount"])})
record("resume", "TC5 after reviewer corrects amount", "COMPLETED_PENDING_ADJUSTER", state)

# ---- Part 4: final adjuster decision (H2, simulated) ----
state = adjuster_decision("TC1", "APPROVE", reviewer="adjuster_1", note="documents complete")
record("decision", "TC1 adjuster approves", "APPROVED", state)

# ---- Summary ----
df = pd.DataFrame(rows)
df.to_csv("results/test_results.csv", index=False)
main = df[df["test"] == "main"]
done = main[main["actual"] == "COMPLETED_PENDING_ADJUSTER"]
print("\n================ TEST RESULTS ================")
print(df[["test", "claim_id", "expected", "actual", "pass", "stopped_at", "pipeline_s", "llm_calls"]].to_string(index=False))
print("\n================ SUMMARY ================")
print(f"Scenarios passed:                     {int(df['pass'].sum())}/{len(df)}")
print(f"Main claims: completed / to human / paused: "
      f"{(main['actual'] == 'COMPLETED_PENDING_ADJUSTER').sum()} / {(main['actual'] == 'ROUTED_TO_HUMAN').sum()} / "
      f"{(main['actual'] == 'PAUSED_MISSING_INFO').sum()}")
if len(done):
    print(f"Avg processing time per completed claim: {done['pipeline_s'].mean():.1f} s "
          f"(manual baseline: 3-5 business days)")
    print(f"Avg LLM calls / output tokens per completed claim: {done['llm_calls'].mean():.1f} / {done['output_tokens'].mean():.0f}")
print("Saved: results/test_results.csv, logs/run_log.jsonl, states/, outputs/")