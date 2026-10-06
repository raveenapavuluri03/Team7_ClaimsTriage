"""Trains the fraud risk model and compares it with the rule-based flags on the same test split."""
import os
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
from src.data_prep import load_claims
from src.risk import train_and_save, rule_based_score, MODEL_PATH

df = load_claims()
model, (X_test, y_test) = train_and_save(df)

ml_prob = model.predict_proba(X_test)[:, 1]
rule_prob = [rule_based_score(df.loc[i].to_dict())[0] for i in X_test.index]


def metrics(name, y, prob, threshold=0.5):
    pred = [int(p >= threshold) for p in prob]
    return {"method": name,
            "accuracy": round(accuracy_score(y, pred), 3),
            "precision": round(precision_score(y, pred, zero_division=0), 3),
            "recall": round(recall_score(y, pred), 3),
            "f1": round(f1_score(y, pred), 3),
            "roc_auc": round(roc_auc_score(y, prob), 3)}


results = pd.DataFrame([metrics("ML: Logistic Regression", y_test, ml_prob),
                        metrics("Rule-based flags", y_test, rule_prob)])
os.makedirs("results", exist_ok=True)
results.to_csv(os.path.join("results", "risk_model_comparison.csv"), index=False)

print(f"Model saved to {MODEL_PATH}")
print(f"Test set: {len(y_test)} claims ({int(y_test.sum())} fraud)\n")
print(results.to_string(index=False))
print("\nSaved: results/risk_model_comparison.csv")