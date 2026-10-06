from src.llm import llm, list_models
from src.data_prep import load_claims

print("Available models:")
try:
    for m in list_models():
        print("  -", m)
except Exception as e:
    print("  (could not list models:", e, ")")

print("\nLLM test:", llm("Reply with exactly: LLM connection OK"))

df = load_claims()
print("\nDataset shape:", df.shape)
print("Fraud counts:\n", df["fraud_reported"].value_counts())
missing = df.isna().sum()
print("Columns with missing values:\n", missing[missing > 0])