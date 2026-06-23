import streamlit as st
import pandas as pd
from pypdf import PdfReader
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import re
import io
import os
import difflib # NEW: Added for Human-like Fuzzy Matching

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

def get_match_score(target: str, candidate: str) -> float:
    """Calculates a mathematical similarity percentage between two names."""
    if not target or not candidate: 
        return 0.0
    if target == candidate: 
        return 1.0
    if len(target) >= 5 and (target in candidate or candidate in target): 
        return 1.0
    # Returns a decimal score (e.g. 0.88) based on character similarity 
    return difflib.SequenceMatcher(None, target, candidate).ratio()

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
        
        inv_num = pdf_file.name.replace(".pdf", "")
        inv_match = re.search(r"(INV-\d+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = inv_match.group(1).strip() if inv_match else inv_num
        
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
        
        cust_match = re.search(r"Customer\s*Name[\s\:]*([A-Za-z0-9\s\.\,\&\-]+?)(?:\s+(?:Invoice|Date|Amount|Terms|Bill\s*To|Ship\s*To|$))", full_text_clean, re.IGNORECASE)
        if cust_match:
            result["customer_name"] = cust_match.group(1).strip()
            
        bill_to_match = re.search(r"Bill\s+To[\s\:]*([A-Za-z0-9\s\.\,\-]+?)(?:\s*\d|\s*Ship\s*To|$)", full_text_clean, re.IGNORECASE)
        if bill_to_match:
            result["fallback_personal_name"] = bill_to_match.group(1).strip()
            if not result["customer_name"]:
                result["customer_name"] = result["fallback_personal_name"]
                
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
    """Isolates the name by mathematically stripping all numbers from the PDF line."""
    records = []
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        for line in full_text.split('\n'):
            if "INV-" in line.upper():
                inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", line, re.IGNORECASE)
                if not inv_match:
                    continue
                inv_id = inv_match.group(1).upper()
                
                after_inv_text = line[inv_match.end():]
                amounts = re.findall(r"[-+]?[0-9,]+\.\d{2}", after_inv_text)
                if not amounts:
                    continue
                    
                float_amounts = [abs(clean_numeric_value(a)) for a in amounts]
                if len(float_amounts) >= 2:
                    gross = max(float_amounts)
                    fee = min(float_amounts)
                elif len(float_amounts) == 1:
                    gross = float_amounts[0]
                    fee = 0.0
                
                # PURIFIER: Delete all money numbers from the line to perfectly isolate the pure text name
                name_candidate = after_inv_text
                for amt in amounts:
                    name_candidate = name_candidate.replace(amt, '')
                    
                cust_name = name_candidate.replace('$', '').replace(',', '').strip()
                
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

# =====================================================================
# 4. STREAMLIT INTERFACE SETUP
# =====================================================================
st.set_page_config(page_title="D365 General Journal Automation", layout="wide")
st.title("D365 General Journal Automation Engine")
st.subheader("Daily Operational Reconciliations Matrix")

possible_paths = ["Account Masterlist.xlsx", "Account Masterlist.csv"]
MASTERLIST_PATH = next((p for p in possible_paths if os.path.exists(p)), None)

if not MASTERLIST_PATH:
    st.error("❌ Core configuration file `Account Masterlist.xlsx` or `.csv` missing from your repository root folder.")
    st.stop()

st.sidebar.header("📅 Daily Variable Inputs")
boa_file = st.sidebar.file_uploader("1. Bank of America Report (Excel/CSV)", type=["xlsx", "csv"])
zoho_file = st.sidebar.file_uploader("2. Zoho Transaction Summary or Direct Invoices (PDF/Excel/CSV)", type=["pdf", "xlsx", "csv"])
uploaded_invoices = st.sidebar.file_uploader("3. Extra Customer Invoices (PDFs) [Optional]", type=["pdf"], accept_multiple_files=True)

if not (boa_file and zoho_file):
    st.info("💡 Staging required: Please drop today's Bank of America report and matching Zoho summary sheet into the sidebar container panel.")
else:
    # -----------------------------------------------------------------
    # STEP A: LOAD MASTERLIST
    # -----------------------------------------------------------------
    if MASTERLIST_PATH.endswith('.csv'):
        master_df = pd.read_csv(MASTERLIST_PATH)
    else:
        master_df = pd.read_excel(MASTERLIST_PATH)
        
    master_df.columns = [str(col).strip() for col in master_df.columns]
    master_headers_lower = {str(col).lower(): str(col) for col in master_df.columns}
    
    ml_name_col = next((master_headers_lower[k] for k in ['account name', 'name', 'customer name'] if k in master_headers_lower), None)
    ml_num_col = next((master_headers_lower[k] for k in ['account #', 'account number', 'account no', 'account'] if k in master_headers_lower), None)
    ml_term_col = next((master_headers_lower[k] for k in ['payment term', 'payment terms', 'terms'] if k in master_headers_lower), None)
    ml_ticket_col = next((master_headers_lower[k] for k in ['cs/ps ticket', 'ticket', 'cs/ps'] if k in master_headers_lower), None)
    
    if not ml_name_col or not ml_num_col:
        st.error("❌ Could not identify definitive baseline 'Account Name' or 'Account #' tracking headers inside Masterlist spreadsheet.")
        st.stop()
        
    master_lookup: Dict[str, AccountMasterItem] = {}
    for _, row in master_df.iterrows():
        name_val = str(row[ml_name_col]).strip()
        num_val = str(row[ml_num_col]).strip()
        term_val = str(row.get(ml_term_col, 'due-on-receipt')).strip().lower() if ml_term_col else 'due-on-receipt'
        ticket_val = str(row.get(ml_ticket_col, '')).strip() if ml_ticket_col else ''
        
        master_lookup[name_val] = AccountMasterItem(
            account_number=num_val,
            account_name=name_val,
            payment_term=term_val,
            norm_name=normalize_name(name_val),
            norm_ticket=normalize_name(ticket_val)
        )

    # -----------------------------------------------------------------
    # STEP B: EXTRACT INVOICES
    # -----------------------------------------------------------------
    invoice_cache = {}
    invoice_sources_list = []
    
    if uploaded_invoices:
        for inv in uploaded_invoices:
            meta = extract_invoice_metadata_intelligent(inv)
            if meta["invoice_number"]:
                invoice_cache[meta["invoice_number"]] = {
                    "resolved_name": meta["customer_name"],
                    "fallback_personal_name": meta["fallback_personal_name"]
                }
                invoice_sources_list.append(ZohoRecord(
                    customer_name=meta["customer_name"],
                    gross_amount=meta["gross_amount"],
                    merchant_fee=0.0,
                    invoice_number=meta["invoice_number"],
                    fallback_personal_name=meta["fallback_personal_name"]
                ))

    # -----------------------------------------------------------------
    # STEP C: PARSE BOA REPORT
    # -----------------------------------------------------------------
    if boa_file.name.endswith('.csv'):
        raw_bytes = boa_file.read()
        lines = raw_bytes.decode('utf-8').splitlines()
        boa_file.seek(0)
        
        skip_count = 0
        for idx, line in enumerate(lines):
            if "date" in line.lower() and "description" in line.lower():
                skip_count = idx
                break
        boa_df = pd.read_csv(boa_file, skiprows=skip_count)
    else:
        boa_df = pd.read_excel(boa_file)
    
    boa_df.columns = [str(col).strip().lower() for col in boa_df.columns]
    desc_target = next((c for c in ['description', 'transaction description', 'payee', 'memo'] if c in boa_df.columns), None)
    date_target = next((c for c in ['posting date', 'date', 'transaction date'] if c in boa_df.columns), None)
    amount_target = next((c for c in ['net amount', 'amount', 'net_amount'] if c in boa_df.columns), None)
    account_target = next((c for c in ['source account', 'account', 'account number', 'account_number'] if c in boa_df.columns), None)

    boa_records: List[BOARecord] = []
    for _, row in boa_df.iterrows():
        row_description = str(row.get(desc_target, ''))
        row_net_amount = clean_numeric_value(row.get(amount_target, 0.0))
        
        if "ZOHO PAYMENTS" in row_description.upper() and row_net_amount > 0:
            parsed_date = datetime.today().strftime('%m/%d/%Y')
            if date_target and pd.notna(row[date_target]):
                try:
                    parsed_date = pd.to_datetime(row[date_target]).strftime('%m/%d/%Y')
                except Exception:
                    pass
            boa_records.append(BOARecord(
                date=parsed_date,
                description=row_description,
                net_amount=row_net_amount,
                source_account=str(row.get(account_target, '')).strip() if account_target else "3371"
            ))

    # -----------------------------------------------------------------
    # STEP D: PARSE ZOHO
    # -----------------------------------------------------------------
    raw_zoho_pool: List[ZohoRecord] = []
    if zoho_file.name.endswith('.pdf'):
        raw_zoho_pool = parse_zoho_summary_pdf_bulletproof(zoho_file)
    else:
        if zoho_file.name.endswith('.csv'):
            zoho_df = pd.read_csv(zoho_file)
        else:
            zoho_df = pd.read_excel(zoho_file)
            
        zoho_df.columns = [str(c).strip() for c in zoho_df.columns]
        
        cust_col = next((c for c in zoho_df.columns if 'customer' in c.lower()), None)
        gross_col = next((c for c in zoho_df.columns if 'gross' in c.lower() or ('amount' in c.lower() and 'net' not in c.lower() and 'fee' not in c.lower())), None)
        fee_col = next((c for c in zoho_df.columns if 'fee' in c.lower()), None)
        inv_col = next((c for c in zoho_df.columns if 'invoice' in c.lower()), None)
        
        for _, row in zoho_df.iterrows():
            c_name = str(row[cust_col]).strip() if cust_col and pd.notna(row[cust_col]) else None
            gross = clean_numeric_value(row[gross_col]) if gross_col else 0.0
            fee = clean_numeric_value(row[fee_col]) if fee_col else 0.0
            inv = str(row[inv_col]).strip() if inv_col and pd.notna(row[inv_col]) else None
            
            raw_zoho_pool.append(ZohoRecord(
                customer_name=c_name,
                gross_amount=gross,
                merchant_fee=fee,
                invoice_number=inv
            ))

    zoho_deduped_dict = {}
    
    for r in raw_zoho_pool:
        if r.invoice_number:
            zoho_deduped_dict[r.invoice_number] = r
            
    for inv_rec in invoice_sources_list:
        if inv_rec.invoice_number:
            if inv_rec.invoice_number in zoho_deduped_dict:
                existing = zoho_deduped_dict[inv_rec.invoice_number]
                if not existing.customer_name and inv_rec.customer_name:
                    existing.customer_name = inv_rec.customer_name
                if not existing.fallback_personal_name and inv_rec.fallback_personal_name:
                    existing.fallback_personal_name = inv_rec.fallback_personal_name
                if inv_rec.gross_amount > 0:
                    existing.gross_amount = inv_rec.gross_amount
            else:
                zoho_deduped_dict[inv_rec.invoice_number] = inv_rec
                
    zoho_records = list(zoho_deduped_dict.values())

    # =====================================================================
    # STEP E: TRANSACTION PROCESSING
    # =====================================================================
    all_journal_lines = []
    validation_errors = []
    diagnostic_logs = []

    for boa_rec in boa_records:
        matched_zoho = [z for z in zoho_records if z.gross_amount > 0]
        if not matched_zoho:
            continue

        total_gross = sum(z.gross_amount for z in matched_zoho)
        total_fees = round(total_gross - boa_rec.net_amount, 2)
        
        if len(matched_zoho) >= 1:
            each_fee = round(total_fees / len(matched_zoho), 2)
            for z in matched_zoho:
                z.merchant_fee = each_fee

        if total_gross == 0:
            validation_errors.append("⚠️ **Data Ingestion Alert:** System failed to extract gross amounts.")
            continue
            
        if total_fees < 0:
            validation_errors.append(f"🚨 **Mathematical Balance Discrepancy!** Bank Net is greater than Gross.")
            continue

        offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "B1000002")
        processed_accounts = []
        
        for z_rec in matched_zoho:
            current_boa_description = str(boa_rec.description)
            
            norm_biz = normalize_name(z_rec.customer_name)
            norm_per = normalize_name(z_rec.fallback_personal_name)
            
            matched_master_item = None
            best_score = 0.0
            best_candidate = "No Close Matches"
            
            # CORE FIX: The AI Fuzzy Matching Loop
            for item in master_lookup.values():
                s1 = get_match_score(norm_biz, item.norm_name)
                s2 = get_match_score(norm_per, item.norm_name)
                s3 = get_match_score(norm_biz, item.norm_ticket) if item.norm_ticket else 0.0
                s4 = get_match_score(norm_per, item.norm_ticket) if item.norm_ticket else 0.0
                
                highest_sim_score = max(s1, s2, s3, s4)
                
                # Keep track of the closest match for the debugger
                if highest_sim_score > best_score:
                    best_score = highest_sim_score
                    best_candidate = item.account_name
                
                # If similarity is 85% or higher, count it as an exact match!
                if highest_sim_score >= 0.85:
                    matched_master_item = item
                    break

            if not matched_master_item:
                account_num = "21040102-B1000002"
                account_type = "Ledger"
                account_name = "Temporary Receipt"
                cash_code = "AR012"
                
                display_label = z_rec.customer_name if z_rec.customer_name else (z_rec.fallback_personal_name if z_rec.fallback_personal_name else "Unknown")
                desc = f"{display_label} (UNRECORDED ENTITY)_{current_boa_description}"
                
                # Upgraded Diagnostic Logger
                diagnostic_logs.append({
                    "Invoice": z_rec.invoice_number,
                    "Raw Name Extracted": z_rec.customer_name,
                    "Engine's Target": norm_biz,
                    "Closest Masterlist Match": f"{best_candidate} ({round(best_score * 100, 1)}% Similarity)"
                })
            else:
                master_item = matched_master_item
                processed_accounts.append(master_item)
                
                term_info = CASH_CODE_MAPPING.get(master_item.payment_term, CASH_CODE_MAPPING['fallback'])
                cash_code = term_info[0]
                prefix = "MPP " if cash_code == "AR002" else ""
                
                account_num = master_item.account_number
                account_type = "Customer"
                account_name = master_item.account_name
                desc = f"{prefix}{account_num} {account_name}_{current_boa_description}"
            
            all_journal_lines.append({
                "Date": boa_rec.date, "Voucher": "", "Account name": account_name,
                "Company": "bwa", "Account type": account_type, "Account": account_num,
                "Posting Profile": "AutoPost" if account_type == "Customer" else "", "Cash code": cash_code, "Description": desc,
                "Debit": "", "Credit": z_rec.gross_amount, "Item sales tax group": "", "Sales tax code": "",
                "Offset company": "bwa", "Bank Account Type": "Bank", "Offset account": offset_acct,
                "Offset transaction text": "", "Currency": "USD", "Exchange rate": 1.00,
                "Item sales tax group2": "", "Sales tax group": "AVATAX", "Withholding tax group": "",
                "Release date": "", "Reversing entry": "No", "Reversing date": ""
            })

        if total_fees > 0:
            current_boa_description = str(boa_rec.description)
            if len(processed_accounts) == 1:
                acc = processed_accounts[0]
                fee_desc = f"Zoho Merchant Fee {acc.account_number} {acc.account_name}_{current_boa_description}"
            elif len(processed_accounts) > 1:
                account_strings = ", ".join([f"{a.account_number} {a.account_name}" for a in processed_accounts])
                fee_desc = f"Zoho Merchant Fee {account_strings}_{current_boa_description}"
            else:
                fee_desc = f"Zoho Merchant Fee (Unresolved Suspense Pool Batch)_{current_boa_description}"

            all_journal_lines.append({
                "Date": boa_rec.date, "Voucher": "", "Account name": "Outside Service (Finance)",
                "Company": "bwa", "Account type": "Ledger", "Account": "43170111-U26C05001-B735350-UOA003",
                "Posting Profile": "", "Cash code": "OSF005", "Description": fee_desc,
                "Debit": total_fees, "Credit": "", "Item sales tax group": "", "Sales tax code": "",
                "Offset company": "bwa", "Bank Account Type": "Bank", "Offset account": offset_acct,
                "Offset transaction text": "", "Currency": "USD", "Exchange rate": 1.00,
                "Item sales tax group2": "", "Sales tax group": "AVATAX", "Withholding tax group": "",
                "Release date": "", "Reversing entry": "No", "Reversing date": ""
            })

    # STEP F: DISPLAY INTERACTIVE METRICS & EXPORT DOWNLOADS
    if validation_errors:
        st.error("### Pipeline Validation Discrepancies Checked")
        for error in validation_errors:
            st.markdown(error)

    if all_journal_lines:
        st.success(f"### Transformed {len(all_journal_lines)} Journal Lines Successfully!")
        output_df = pd.DataFrame(all_journal_lines, columns=D365_TEMPLATE_COLUMNS)
        st.dataframe(output_df)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            output_df.to_excel(writer, index=False, sheet_name="Journal Lines")
        
        st.download_button(
            label="📥 Download Generated D365 Journal Import Sheet",
            data=buffer.getvalue(),
            file_name="D365_General_Journal_Import.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
    # =====================================================================
    # DIAGNOSTIC DASHBOARD 
    # =====================================================================
    if diagnostic_logs:
        st.markdown("---")
        with st.expander("🚨 🕵️ Unmatched Entities Debugger (Click Here)", expanded=True):
            st.error("**Why are these showing as Temporary Receipt?** \nThe AI couldn't find a strong enough match (requires 85% similarity). Look at the 'Closest Masterlist Match' column below to see what it almost matched with. If the score is low, verify the Masterlist has the correct spelling.")
            st.dataframe(pd.DataFrame(diagnostic_logs))
