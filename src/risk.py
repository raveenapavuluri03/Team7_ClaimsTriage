"""S6-S8: fraud/risk features, ML risk score (main) and rule-based flags (fallback + comparison)."""
import os
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

MODEL_PATH = os.path.join("models", "risk_model.joblib")

# Claim/policy features only. Personal attributes (sex, education, occupation, hobbies,
# relationship) are deliberately excluded to avoid unfair proxies.
NUM_FEATURES = ["months_as_customer", "age", "policy_deductable", "policy_annual_premium",
                "umbrella_limit", "incident_hour_of_the_day", "number_of_vehicles_involved",
                "bodily_injuries", "witnesses", "total_claim_amount", "injury_claim",
                "property_claim", "vehicle_claim", "auto_year"]
CAT_FEATURES = ["incident_type", "collision_type", "incident_severity", "authorities_contacted",
                "police_report_available", "property_damage"]


def build_features(record):
    """S6: turn one claim record (dict) or a DataFrame into the model's feature table."""
    X = pd.DataFrame([record]) if isinstance(record, dict) else record.copy()
    for c in NUM_FEATURES:
        X[c] = pd.to_numeric(X.get(c), errors="coerce")
    for c in CAT_FEATURES:
        X[c] = X.get(c).astype(object).where(X.get(c).notna(), "MISSING").astype(str)
    return X[NUM_FEATURES + CAT_FEATURES]


def make_model():
    prep = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), NUM_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_FEATURES),
    ])
    return Pipeline([("prep", prep),
                     ("clf", LogisticRegression(class_weight="balanced", max_iter=2000))])


def split(df):
    X, y = build_features(df), df["fraud_reported"]
    return train_test_split(X, y, test_size=0.2, stratify=y, random_state=42)


def train_and_save(df):
    X_train, X_test, y_train, y_test = split(df)
    model = make_model().fit(X_train, y_train)
    os.makedirs("models", exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    return model, (X_test, y_test)


def load_model(df=None):
    if os.path.exists(MODEL_PATH):
        return joblib.load(MODEL_PATH)
    if df is None:
        raise FileNotFoundError("No saved model. Run: python train_risk_model.py")
    return train_and_save(df)[0]


def rule_based_score(record):
    """Transparent rule flags. Returns a 0-1 score and the list of triggered flags."""
    r = record
    flags = []
    if r.get("incident_severity") == "Major Damage":
        flags.append(("Major damage reported", 0.35))
    if r.get("police_report_available") != "YES":
        flags.append(("No police report on file", 0.15))
    if not r.get("witnesses"):
        flags.append(("No witnesses", 0.10))
    if (r.get("total_claim_amount") or 0) > 60_000:
        flags.append(("Claim amount above $60,000", 0.15))
    if pd.isna(r.get("authorities_contacted")) or r.get("authorities_contacted") in ("None", "MISSING"):
        flags.append(("No authorities contacted", 0.10))
    if (r.get("months_as_customer") or 999) < 12:
        flags.append(("Customer for less than 12 months", 0.15))
    score = min(1.0, sum(w for _, w in flags))
    return round(score, 3), [f for f, _ in flags]


def risk_band(score):
    return "HIGH" if score >= 0.7 else "MEDIUM" if score >= 0.4 else "LOW"


def ml_score(model, record):
    """S7: probability that the claim is fraudulent according to the ML model."""
    return round(float(model.predict_proba(build_features(record))[0, 1]), 3)


def validate_score(score):
    """S8: the risk output must be a real number between 0 and 1."""
    return isinstance(score, (int, float)) and not np.isnan(score) and 0.0 <= score <= 1.0