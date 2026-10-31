"""Production cost model built from MEASURED token counts in states/*.json.
Usage: python cost_model.py [claims_per_month]      (default assumption: 3000)
Rates are inputs; change them below if prices change. Sources are listed in the report."""
import glob
import json
import sys

import pandas as pd

VOLUME = int(sys.argv[1]) if len(sys.argv) > 1 else 3000       # ASSUMPTION: claims per month

# ---- rates (October 2026) ----
API_IN_PER_M, API_OUT_PER_M = 0.05, 0.20       # USD per 1M tokens, hosted gpt-oss-20b (aggregator page citing OpenAI)
GPU_PER_HOUR = 1.006                           # AWS g5.xlarge (1x A10G, 24 GB), on-demand, us-east-1
HOURS_24x7, HOURS_BUSINESS = 730, 8 * 22       # hours per month: always on / weekdays 8 h
# ---- other cost ASSUMPTIONS (stated in the report) ----
REVIEW_MINUTES, REVIEWER_RATE = 6, 45.0        # adjuster minutes per reviewed claim, loaded USD per hour
APP_HOST, DB_AND_LOGS = 30.0, 25.0             # small VM for the workflow + managed database/log storage, USD per month

rows = []
for path in glob.glob("states/*.json"):
    s = json.load(open(path, encoding="utf-8"))
    if s.get("status") in ("COMPLETED_PENDING_ADJUSTER", "PAID", "DENIED", "APPROVED_EXECUTING", "WITHHELD_EXPIRED",
                           "ESCALATED_TO_SUPERVISOR") and s["metrics"]["llm_calls"] > 0 and s["claim_id"].startswith("TC") \
            and "-" not in s["claim_id"]:
        m = s["metrics"]
        rows.append({"claim": s["claim_id"], "llm_calls": m["llm_calls"], "prompt_tokens": m["prompt_tokens"],
                     "output_tokens": m["output_tokens"], "llm_seconds": m["llm_latency_ms"] / 1000})
if not rows:
    sys.exit("No completed claims found in states/. Run python run_tests.py first.")
df = pd.DataFrame(rows)
avg = df.mean(numeric_only=True)
tin, tout = avg["prompt_tokens"], avg["output_tokens"]

api_per_claim = tin / 1e6 * API_IN_PER_M + tout / 1e6 * API_OUT_PER_M
review_per_claim = REVIEW_MINUTES / 60 * REVIEWER_RATE
gpu24, gpu_bus = GPU_PER_HOUR * HOURS_24x7, GPU_PER_HOUR * HOURS_BUSINESS
fixed = APP_HOST + DB_AND_LOGS

opts = [
    ("A. Hosted per-token API", api_per_claim * VOLUME),
    ("B. Own GPU server, business hours", gpu_bus),
    ("C. Own GPU server, 24x7", gpu24),
    ("D. Laptop / existing hardware (prototype)", 0.0),
]
print(f"Completed claims measured: {len(df)}   (volume assumption: {VOLUME:,} claims/month)")
print(f"Average per completed claim: {avg['llm_calls']:.1f} LLM calls, {tin:,.0f} input tokens, {tout:,.0f} output tokens, "
      f"{avg['llm_seconds']:.1f} s model time")
print(f"\nToken cost per claim at hosted rates (${API_IN_PER_M}/M in, ${API_OUT_PER_M}/M out): ${api_per_claim:.5f}")
out = []
for name, model_cost in opts:
    total_machine = model_cost + fixed
    out.append({"option": name, "model_cost_per_month": round(model_cost, 2),
                "fixed_app_db_logs": fixed, "machine_total_per_month": round(total_machine, 2),
                "machine_cost_per_claim": round(total_machine / VOLUME, 4),
                "human_review_per_claim": round(review_per_claim, 2),
                "all_in_per_claim": round(total_machine / VOLUME + review_per_claim, 2),
                "all_in_per_month": round(total_machine + review_per_claim * VOLUME, 2)})
res = pd.DataFrame(out)
print("\n" + res.to_string(index=False))
be = gpu24 / api_per_claim if api_per_claim else float("inf")
print(f"\nBreak-even: a 24x7 GPU only beats per-token pricing above ~{be:,.0f} claims/month.")
print(f"Human review is the largest cost: ${review_per_claim:.2f} per claim vs ${api_per_claim:.5f} of tokens.")
res.to_csv("results/cost_model.csv", index=False)
print("Saved results/cost_model.csv")