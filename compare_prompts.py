"""Measured comparison of two extraction prompts on the same narratives.
Format reliability = strict pipeline validation. Accuracy = lenient parse scored against the answer key."""
import json
import os
import re
import pandas as pd
from src.extraction import build_prompt, parse_and_validate
from src.llm import llm_with_meta

claims = pd.read_csv("data/test_claims.csv")
# TC2 has no amount and TC5's narrative amount is altered on purpose, so they are excluded here
claims = claims[~claims["claim_id"].isin(["TC2", "TC5"])]


def lenient_parse(text):
    """Pull out the first {...} block and map loosely named keys to our fields (for accuracy only)."""
    m = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for k, v in data.items():
        key = re.sub(r"[^a-z]", "", k.lower())
        if "date" in key:
            out["incident_date"] = v
        elif "type" in key:
            out["incident_type"] = v
        elif "amount" in key:
            out["amount"] = v
        elif "police" in key:
            out["police_report"] = v
        elif "vehicle" in key:
            out["vehicle"] = " ".join(str(x) for x in v.values()) if isinstance(v, dict) else v
    return out


def to_amount(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower()
    num = re.findall(r"[\d.]+", s.replace(",", ""))
    if not num:
        return None
    val = float(num[0])
    return val * 1000 if "thousand" in s else val


def to_police(v):
    if isinstance(v, bool):
        return "YES" if v else "NO"
    s = str(v).upper()
    return "YES" if s in ("YES", "TRUE", "Y") else "NO" if s in ("NO", "FALSE", "N") else "UNKNOWN"


def score(ex, truth):
    checks = {}
    try:
        checks["date"] = pd.to_datetime(str(ex.get("incident_date"))).strftime("%Y-%m-%d") == truth["true_incident_date"]
    except Exception:
        checks["date"] = False
    checks["type"] = str(ex.get("incident_type", "")).strip().lower() == truth["true_incident_type"].lower()
    a = to_amount(ex.get("amount"))
    checks["amount"] = a is not None and abs(a - truth["true_amount"]) <= 0.01 * truth["true_amount"]
    checks["police"] = to_police(ex.get("police_report")) == truth["true_police_report"]
    veh = str(ex.get("vehicle", "")).lower()
    checks["vehicle"] = all(part.lower() in veh for part in truth["true_vehicle"].split()[:2])
    return checks


rows = []
for version in ["A", "B"]:
    for _, c in claims.iterrows():
        meta = llm_with_meta(build_prompt(c["narrative"], version), json_mode=(version == "B"))
        strict, error = parse_and_validate(meta["text"])
        checks = score(strict or lenient_parse(meta["text"]), c)
        rows.append({"prompt": version, "claim_id": c["claim_id"], "strict_valid": strict is not None,
                     "error": error, **{f"ok_{k}": v for k, v in checks.items()},
                     "field_accuracy": sum(checks.values()) / len(checks),
                     "latency_s": meta["latency_ms"] / 1000, "output_tokens": meta["output_tokens"]})
        print(f"Prompt {version} {c['claim_id']:>4}: valid={strict is not None}  "
              f"fields={sum(checks.values())}/{len(checks)}  {meta['latency_ms']/1000:.1f}s")

detail = pd.DataFrame(rows)
summary = detail.groupby("prompt").agg(
    claims=("claim_id", "count"),
    valid_first_try=("strict_valid", "mean"),
    field_accuracy=("field_accuracy", "mean"),
    date_acc=("ok_date", "mean"), type_acc=("ok_type", "mean"), amount_acc=("ok_amount", "mean"),
    police_acc=("ok_police", "mean"), vehicle_acc=("ok_vehicle", "mean"),
    avg_latency_s=("latency_s", "mean"), avg_output_tokens=("output_tokens", "mean"),
).round(3).reset_index()

os.makedirs("results", exist_ok=True)
detail.to_csv("results/prompt_comparison_detail.csv", index=False)
summary.to_csv("results/prompt_comparison_summary.csv", index=False)
print("\n================ PROMPT A vs B ================")
print(summary.to_string(index=False))
print("\nSaved: results/prompt_comparison_summary.csv and results/prompt_comparison_detail.csv")