import pandas as pd
import re
from core.models import ZohoRecord
from typing import List, Any

# Helper function to prevent float() crashes on dollar signs/commas
def clean_numeric_value(val: Any) -> float:
    if pd.isna(val) or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned_str = str(val).strip().replace('$', '').replace(',', '')
    try:
        return float(cleaned_str)
    except ValueError:
        return 0.0

class ZohoParser:
    @staticmethod
    def parse_summary(file_path: str) -> List[ZohoRecord]:
        """Parses a Zoho summary export (Excel/CSV supported) safely isolating refunds."""
        records = []
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
            
        # Strip whitespace from column headers to prevent mismatch errors
        df.columns = [str(c).strip() for c in df.columns]
        
        # Dynamic header hunting
        cust_col = next((c for c in df.columns if 'customer' in c.lower()), None)
        gross_col = next((c for c in df.columns if 'gross' in c.lower() or ('amount' in c.lower() and 'net' not in c.lower() and 'fee' not in c.lower())), None)
        fee_col = next((c for c in df.columns if 'fee' in c.lower()), None)
        inv_col = next((c for c in df.columns if 'invoice' in c.lower()), None)
        
        for _, row in df.iterrows():
            c_name = str(row[cust_col]).strip() if cust_col and pd.notna(row[cust_col]) else None
            inv = str(row[inv_col]).strip() if inv_col and pd.notna(row[inv_col]) else None
            
            # Read the raw string values to look for explicit minus signs
            raw_gross_val = row[gross_col] if gross_col else 0.0
            raw_fee_val = row[fee_col] if fee_col else 0.0
            
            # CORE PROTECTION RULE: If either column contains a negative sign, it is a deduction/refund.
            # We explicitly skip or flag it, guaranteeing it never gets combined into a merchant fee.
            if '-' in str(raw_gross_val) or '-' in str(raw_fee_val):
                # This is a refund entry! Skip combining it into the processing fees
                continue
                
            gross = clean_numeric_value(raw_gross_val)
            fee = clean_numeric_value(raw_fee_val)
            
            if gross > 0:
                records.append(ZohoRecord(
                    customer_name=c_name,
                    gross_amount=gross,
                    merchant_fee=fee, # This will now be strictly the $57.25 true processing fee charge
                    invoice_number=inv
                ))
            
        return records
