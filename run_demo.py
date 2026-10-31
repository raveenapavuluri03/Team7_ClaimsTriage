"""Runs the workflow on one claim, then lets you act as the reviewer. Usage: python run_demo.py TC1"""
import sys
import pandas as pd
from src import review
from src.pipeline import run_claim
from src.state import load_state

claim_id = sys.argv[1] if len(sys.argv) > 1 else "TC1"
claims = pd.read_csv("data/test_claims.csv")
row = claims[claims["claim_id"] == claim_id].iloc[0].to_dict()
truth = {k: row.pop(k) for k in list(row) if k.startswith("true_") or k in ("expected_status", "case_type", "fraud_reported")}
force_bad_json = bool(row.pop("force_bad_json"))

state = run_claim(row, force_bad_json=force_bad_json)
print(f"\nFinal status: {state['status']}  (expected: {truth['expected_status']})")
print("Reason:", state["status_reason"])
print("Metrics:", state["metrics"])

if state["status"] == "COMPLETED_PENDING_ADJUSTER":
    rid = state["results"]["S10"]["review_id"]
    print("\n" + review.render_review(rid))
    answer = input("\nReviewer decision (approve / reject / skip): ").strip().lower()
    if answer in ("approve", "reject"):
        ack = bool(review.get_review(rid)["fallback_score"])      # the demo reviewer acknowledges a fallback score
        ev = review.make_event(rid, answer, "adjuster_1", "decided in demo", ack_fallback=ack)
        print("Event outcome:", review.handle_event(ev))
        print("Claim status:", load_state(claim_id)["status"])