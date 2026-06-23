import pandas as pd
from datetime import datetime
from core.models import BOARecord
from typing import List

class BOAParser:
    @staticmethod
    def parse_csv(file_path: str) -> List[BOARecord]:
        """Parses and isolates Zoho transactions out of the Bank of America CSV export."""
        records = []
        df = pd.read_csv(file_path)
        
        for _, row in df.iterrows():
            desc = str(row.get('Description', '')).upper()
            
            # Rule 3.1: Filtering Pipeline
            if "ZOHO" in desc:
                # Raw Parsing without extrapolation
                raw_date = row.get('Posting Date')
                parsed_date = datetime.strptime(str(raw_date), "%Y-%m-%d").date() if pd.notna(raw_date) else None
                
                records.append(BOARecord(
                    date=parsed_date,
                    description=str(row.get('Description', '')),
                    net_amount=float(row.get('Net Amount', 0.0)),
                    source_account=str(row.get('Source Account', '')).strip()
                ))
        return records
