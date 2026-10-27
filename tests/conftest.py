import pytest

HEADER = ("step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,"
          "nameDest,oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud")
ROWS = [
    "1,PAYMENT,9839.64,C1231006815,170136.0,160296.36,M1979787155,0.0,0.0,0,0",
    "1,TRANSFER,181.0,C1305486145,181.0,0.0,C553264065,0.0,0.0,1,0",
    "2,CASH_OUT,181.0,C840083671,181.0,0.0,C38997010,21182.0,0.0,1,0",
    "2,CASH_IN,5000.5,C2,100.0,5100.5,C3,0.0,0.0,0,0",
    "3,DEBIT,250.0,C4,1000.0,750.0,C5,10.0,260.0,0,0",
    "3,TRANSFER,300000.0,C6,300000.0,0.0,C7,0.0,0.0,1,1",
]


@pytest.fixture
def make_csv(tmp_path):
    def _make(rows=ROWS, header=HEADER, name="paysim.csv"):
        p = tmp_path / name
        p.write_text("\n".join([header, *rows]) + "\n")
        return p
    return _make
