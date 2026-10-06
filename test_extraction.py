"""Quick check of S3 + S4 on TC1 with both prompts, plus a forced invalid output."""
import pandas as pd
from src.llm import llm_with_meta
from src.extraction import build_prompt, parse_and_validate, cross_check

claims = pd.read_csv("data/test_claims.csv")
c = claims[claims["claim_id"] == "TC1"].iloc[0].to_dict()

print("NARRATIVE:\n", c["narrative"], "\n")
print("TRUE VALUES:", {k: c[k] for k in ["true_incident_date", "true_incident_type",
                                          "true_amount", "true_police_report", "true_vehicle"]}, "\n")

for version in ["A", "B"]:
    meta = llm_with_meta(build_prompt(c["narrative"], version), json_mode=(version == "B"))
    parsed, error = parse_and_validate(meta["text"])
    print(f"--- Prompt {version} ({meta['latency_ms']} ms, {meta['output_tokens']} output tokens) ---")
    print("Raw output:", meta["text"][:400])
    print("Valid:", parsed is not None, "| Error:", error)
    if parsed:
        print("Extracted:", parsed)
        print("Cross-check issues:", cross_check(parsed, c) or "none")
    print()

print("--- Forced invalid output (failure path test) ---")
print(parse_and_validate("Sorry, I could not process this claim."))