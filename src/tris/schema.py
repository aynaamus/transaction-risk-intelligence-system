"""PaySim schema and data-use policy.

PaySim is a *synthetic* dataset. `step` is simulated hours, not a timestamp.
Nothing here supports inferring real money laundering, ownership, geography
or customer identity.
"""

# Source column name -> DuckDB type. Names and order match the original CSV.
COLUMNS: dict[str, str] = {
    "step": "INTEGER",
    "type": "VARCHAR",
    "amount": "DOUBLE",
    "nameOrig": "VARCHAR",
    "oldbalanceOrg": "DOUBLE",
    "newbalanceOrig": "DOUBLE",
    "nameDest": "VARCHAR",
    "oldbalanceDest": "DOUBLE",
    "newbalanceDest": "DOUBLE",
    "isFraud": "TINYINT",
    "isFlaggedFraud": "TINYINT",
}

TABLE = "transactions"
LOG_TABLE = "ingestion_log"

# Simulated ground truth label. Never a detection feature.
LABEL_GROUND_TRUTH = "isFraud"
# Output of the simulator's own rule; not ground truth. Never a detection feature.
LABEL_SIMULATOR_RULE = "isFlaggedFraud"

# Stored for source fidelity only; the dataset authors identify leakage risks.
LEAKAGE_RISK_BALANCE_COLUMNS = (
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
)

# Columns that must never be used as fraud-detection features.
FEATURE_EXCLUDED_COLUMNS = (
    LABEL_GROUND_TRUTH,
    LABEL_SIMULATOR_RULE,
    *LEAKAGE_RISK_BALANCE_COLUMNS,
)

KNOWN_TYPES = ("CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER")

# Columns whose absence makes a row unusable.
CRITICAL_COLUMNS = tuple(COLUMNS)
