import os
from dotenv import load_dotenv

load_dotenv(override=True)

DATA_PATH = os.path.join("data", "insurance_claims.csv")