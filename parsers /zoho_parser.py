import pandas as pd
from core.models import ZohoRecord
from typing import List

class ZohoParser:
    @staticmethod
    def parse_summary(file_path: str) -> List[ZohoRecord]:
        """Parses a Zoho summary export (Excel/CSV supported)."""
        records = []
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
            
        for _, row in df.iterrows():
            records.append(ZohoRecord(
                customer_name=str(row['Customer']) if pd.notna(row.get('Customer')) else None,
                gross_amount=float(row.get('Gross Amount', 0.0)),
                merchant_fee=float(row.get('Merchant Fee', 0.0)),
                invoice_number=str(row['Invoice Number']) if pd.notna(row.get('Invoice Number')) else None
            ))
        return records
