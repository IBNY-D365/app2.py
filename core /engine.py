import pandas as pd
from pypdf import PdfReader
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import re
import streamlit as st

# =====================================================================
# DATA UTILITIES & MODELS
# =====================================================================
class ZohoRecord(BaseModel):
    customer_name: Optional[str] = None
    gross_amount: float
    merchant_fee: float
    invoice_number: Optional[str] = None
    fallback_personal_name: Optional[str] = None

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

# =====================================================================
# ADVANCED EXTRACTION ENGINE
# =====================================================================
def extract_invoice_metadata_intelligent(pdf_file) -> Dict[str, Any]:
    """Scans individual invoices to capture the precise Paid Amount and business entity."""
    result = {"customer_name": None, "invoice_number": None, "gross_amount": 0.0, "fallback_personal_name": None}
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        full_text_clean = " ".join(full_text.split())
        
        # 1. Invoice Number Extraction
        inv_num = pdf_file.name.replace(".pdf", "")
        inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = inv_match.group(1).strip() if inv_match else inv_num
        
        # 2. Gross Amount Extraction
        pm_match = re.search(r"Payment\s*Made[^\d\$]*\$?([0-9,]+\.\d{2})", full_text_clean, re.IGNORECASE)
        if pm_match:
            result["gross_amount"] = clean_numeric_value(pm_match.group(1))
        else:
            totals = re.findall(r"Total[^\d\$]*\$?([0-9,]+\.\d{2})", full_text_clean, re.IGNORECASE)
            if totals:
                result["gross_amount"] = clean_numeric_value(totals[-1])
            else:
                all_decimals = [clean_numeric_value(n) for n in re.findall(r"\b\d+(?:,\d{3})*\.\d{2}\b", full_text_clean)]
                result["gross_amount"] = max(all_decimals) if all_decimals else 0.0
        
        # 3. Aggressive Name Extraction (Looks for Bill To, Customer Name, or just To:)
        bill_to_match = re.search(r"(?:Bill\s*To|Customer\s*Name|To)[\s\:]+([A-Za-z0-9\s\.\,\&\-]+?)(?:\s+(?:Invoice|Date|Amount|Terms|Ship|Receipt|Total|[\$\d]))", full_text_clean, re.IGNORECASE)
        if bill_to_match:
            candidate = bill_to_match.group(1).strip()
            if len(candidate) > 3:
                result["customer_name"] = candidate
                result["fallback_personal_name"] = candidate
                
        # 4. Deep Target Search Fallback
        if not result["customer_name"]:
            biz_matches = re.findall(r"InBody\d*\s*-\s*[^-]+?-\s*([A-Za-z0-9\s\.\,\&]+)", full_text_clean, re.IGNORECASE)
            for match in biz_matches:
                candidate = re.sub(r'\d+\.\d{2}.*', '', match).strip()
                if candidate and not any(k in candidate.lower() for k in ["malfunction", "check required", "sku", "labor", "board", "cable", "loaner"]):
                    result["customer_name"] = candidate
                    break
                    
    except Exception as e:
        st.error(f"Error executing intelligent metadata capture: {e}")
    return result

def parse_zoho_summary_pdf_bulletproof(pdf_file) -> List[ZohoRecord]:
    """Isolates the name by scanning horizontally across the entire PDF blob."""
    records = []
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        # Squashes the entire PDF into one massive block of text to defeat line-break formatting issues
        flat_text = full_text.replace('\n', ' ')
        
        # AGGRESSIVE SEARCH PATTERN:
        # Finds [INV-XXXX] + [Any Words Trapped in the Middle] + [First Money Amount]
        pattern = re.compile(r"(INV-[A-Za-z0-9\-]+)\s+([A-Za-z0-9\s\.\,\&\-]+?)\s+\$?([0-9,]+\.\d{2})")
        matches = pattern.finditer(flat_text)
        
        for match in matches:
            inv_id = match.group(1).upper()
            raw_name = match.group(2).strip()
            gross_str = match.group(3)
            
            # Clean up the extracted name (Strips out accidental dates like 06/10/2026 that snuck in)
            clean_name = re.sub(r'\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b', '', raw_name)
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            
            gross = clean_numeric_value(gross_str)
            
            # Look ahead for the fee (next 50 characters)
            lookahead = flat_text[match.end():match.end()+50]
            fee_matches = re.findall(r"[-+]?[0-9,]+\.\d{2}", lookahead)
            fee = abs(clean_numeric_value(fee_matches[0])) if fee_matches else 0.0
            
            if gross > 0 and not any(r.invoice_number == inv_id for r in records):
                records.append(ZohoRecord(
                    customer_name=clean_name if len(clean_name) > 3 else None,
                    gross_amount=gross,
                    merchant_fee=fee,
                    invoice_number=inv_id
                ))
                
    except Exception as e:
        st.error(f"Error executing summary parser: {e}")
    return records
