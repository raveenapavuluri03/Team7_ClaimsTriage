"""Runs the full workflow on one claim. Usage: python run_demo.py TC1"""
import json
import sys
import pandas as pd
from src.pipeline import run_claim, adjuster_decision

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
    print("\nAdjuster package:")
    print(open(f"outputs/{claim_id}_package.json", encoding="utf-8").read())
    answer = input("\nAdjuster decision (approve / deny / skip): ").strip().lower()
    if answer in ("approve", "deny"):
        adjuster_decision(claim_id, answer, reviewer="demo_adjuster", note="decided in demo")