import numpy as np
import pandas as pd
from src.config import DATA_PATH


def load_claims(path=DATA_PATH):
    """Load and clean the Mendeley insurance_claims dataset."""
    df = pd.read_csv(path)
    df = df.drop(columns=[c for c in df.columns if c.startswith("_c")])  # empty column
    df = df.replace("?", np.nan)
    df["policy_bind_date"] = pd.to_datetime(df["policy_bind_date"])
    df["incident_date"] = pd.to_datetime(df["incident_date"])
    df["fraud_reported"] = df["fraud_reported"].map({"Y": 1, "N": 0})
    return df