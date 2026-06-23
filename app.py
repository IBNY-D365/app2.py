import streamlit as st
import pandas as pd
from pypdf import PdfReader
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import re
import io
import os

# =====================================================================
# 1. HARDCODED CONFIGURATIONS & MAPPINGS
# =====================================================================
CASH_CODE_MAPPING = {
    "due-on-receipt": ("AR001", "AR Collection_AP"),
    "monthly": ("AR002", "AR Collection_MPP"),
    "financing": ("AR003", "AR Collection_Financing"),
    "leasing": ("AR004", "AR Collection_Leasing"),
    "net 1 day": ("AR005", "AR Collection_Net_1Day"),
    "net 10 days": ("AR006", "AR Collection_Net_10Days"),
    "net 25 days": ("AR007", "AR Collection_Net_25Days"),
    "net 30 days": ("AR008", "AR Collection_Net_30Days"),
    "net 40 days": ("AR009", "AR Collection_Net_40Days"),
    "net 45 days": ("AR010", "AR Collection_Net_45Days"),
    "net 60 days": ("AR011", "AR Collection_Net_60Days"),
    "fallback": ("AR012", "AR Collection_Other")
}

OFFSET_ACCOUNT_ROUTING = {
    "3371": "B1000002",
    "3924": "B1000003",
    "3384": "B1000001"
}

D365_TEMPLATE_COLUMNS = [
    "Date", "Voucher", "Account name", "Company", "Account type", "Account",
    "Posting Profile", "Cash code", "Description", "Debit", "Credit",
    "Item sales tax group", "Sales tax code", "Offset company", "Bank Account Type",
    "Offset account", "Offset transaction text", "Currency", "Exchange rate",
    "Item sales tax group2", "Sales tax group", "Withholding tax group",
    "Release date", "Reversing entry", "Reversing date"
]

# =====================================================================
# 2. DATA UTILITIES & MODELS
# =====================================================================
class BOARecord(BaseModel):
    date: Any
    description: str
    net_amount: float
    source_account: str

class ZohoRecord(BaseModel):
    customer_name: Optional[str] = None
    gross_amount: float
    merchant_fee: float
    invoice_number: Optional[str] = None
    fallback_personal_name: Optional[str] = None

class AccountMasterItem(BaseModel):
    account_number: str
    account_name: str
    payment_term: str
    norm_name: str
    norm_ticket: str

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

def normalize_name(name: str) -> str:
    """Removes LLC, INC, and aggressively strips ALL non-alphanumeric characters."""
    if not name or pd.isna(name):
        return ""
    n = str(name).lower()
    n = re.sub(r'\b(inc|llc|corp|ltd|incorporated|company|co|pllc)\b', '', n)
    n = re.sub(r'[^a-z0-9]', '', n)
    return n

# =====================================================================
# 3. ADVANCED EXTRACTION ENGINE
# =====================================================================
def extract_invoice_metadata_intelligent(pdf_file) -> Dict[str, Any]:
    """Scans the invoice to capture the precise Paid Amount and business entity."""
    result = {"customer_name": None, "invoice_number": None, "gross_amount": 0.0, "fallback_personal_name": None}
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        full_text_clean = " ".join(full_text.split())
        
        # 1. Invoice Number Extraction
        inv_num = pdf_file.name.replace(".pdf", "")
        inv_match = re.search(r"(INV-\d+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = inv_match.group(1).strip() if inv_match else inv_num
        
        # 2. Source of Truth Gross Amount Extraction
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
        
        # 3. Robust Customer Name Extraction (UPGRADED)
        
        # Strategy A: Explicit "Customer Name" Field
        cust_match = re.search(r"Customer\s*Name[\s\:]*([A-Za-z0-9\s\.\,\&\-]+?)(?:\s+(?:Invoice|Date|Amount|Terms|Bill\s*To|Ship\s*To|$))", full_text_clean, re.IGNORECASE)
        if cust_match:
            result["customer_name"] = cust_match.group(1).strip()
            
        # Strategy B: "Bill To" Field Fallback
        bill_to_match = re.search(r"Bill\s+To\s*([A-Za-z0-9\s\.\,\-]+?)(?:\s*\d|\s*Ship\s*To|$)", full_text_clean, re.IGNORECASE)
        if bill_to_match:
            result["fallback_personal_name"] = bill_to_match.group(1).strip()
            # If Strategy A failed, use Strategy B as the main name
            if not result["customer_name"]:
                result["customer_name"] = result["fallback_personal_name"]
                
        # Strategy C: Legacy Deep Target Search (InBody Item format)
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
    """Now effectively extracts the string Customer Name from the raw PDF text."""
    records = []
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        # Parse line by line to preserve spacing context for the business name
        for line in full_text.split('\n'):
            if "INV-" in line.upper():
                inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", line, re.IGNORECASE)
                if not inv_match:
                    continue
                inv_id = inv_match.group(1).upper()
                
                amounts = re.findall(r"\b\d+(?:,\d{3})*\.\d{2}\b", line)
                if not amounts:
                    continue
                    
                gross = clean_numeric_value(amounts[0])
                fee = clean_numeric_value(amounts[1]) if len(amounts) > 1 else 0.0
                
                # Slices the text strictly between the INV tag and the first money amount
                after_inv = line[inv_match.end():]
                amount_match = re.search(r"\b\d+(?:,\d{3})*\.\d{2}\b", after_inv)
                cust_name = None
                
                if amount_match:
                    cust_name = after_inv[:amount_match.start()].replace('$', '').strip()
                
                if gross > 0 and not any(r.invoice_number == inv_id for r in records):
                    records.append(ZohoRecord(
                        customer_name=cust_name if cust_name else None,
                        gross_amount=gross,
                        merchant_fee=fee,
                        invoice_number=inv_id
                    ))
    except Exception as e:
        st.error(f"Error executing summary parser: {e}")
    return records
