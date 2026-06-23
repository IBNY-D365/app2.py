from pypdf import PdfReader
import re
from typing import Optional

class InvoiceParser:
    @staticmethod
    def extract_bill_to(pdf_path: str) -> Optional[str]:
        """
        Rule 3.2: Extracts customer name directly out of the 
        physical 'Bill to' box within a customer invoice PDF.
        """
        try:
            reader = PdfReader(pdf_path)
            full_text = ""
            for page in reader.pages:
                full_text += page.extract_text() or ""
            
            # Text normalization parsing zone
            match = re.search(r"Bill\s+to[:]?\s*(.*)", full_text, re.IGNORECASE)
            if match:
                return match.group(1).split('\n')[0].strip()
        except Exception:
            return None
        return None
