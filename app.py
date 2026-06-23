import streamlit as st
import pandas as pd
from pypdf import PdfReader
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import re
import io
import os
import difflib

# =====================================================================
# 1. DETERMINISTIC SYSTEM MAPPINGS & CONFIGURATIONS
# =====================================================================
CASH_CODE_MAPPING = {
    "due-on-receipt": "AR001",
    "monthly": "AR002",
    "financing": "AR003",
    "leasing": "AR004",
    "net 1 day": "AR005",
    "net 10 days": "AR006",
    "net 25 days": "AR007",
    "net 30 days": "AR008",
    "net 40 days": "AR009",
    "net 45 days": "AR010",
    "net 60 days": "AR011",
    "fallback": "AR012"
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
    "Item sales tax group2", "Sales group", "Withholding tax group",
    "Release date", "Reversing entry", "Reversing date"
]

# =====================================================================
# 2. DATA CONTAINERS & HIGH-FIDELITY PARSING UTILITIES
# =====================================================================
class BOARecord(BaseModel):
    date: Any
    description: str
    net_amount: float
    source_account: str

class ZohoRecord(BaseModel):
    customer_name: Optional[str] = None
    gross_amount: float = 0.0
    merchant_fee: float = 0.0
    refund_amount: float = 0.0
    invoice_number: Optional[str] = None
    transaction_type: str  # "payment" or "refund"
    description: str
    transaction_key: str

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
    if not name or pd.isna(name):
        return ""
    n = str(name).lower()
    n = re.sub(r'\b(inc|llc|corp|ltd|incorporated|company|co|pllc)\b', '', n)
    n = re.sub(r'[^a-z0-9]', '', n)
    return n

def get_match_score(target: str, candidate: str) -> float:
    if not target or not candidate: 
        return 0.0
    if target == candidate: 
        return 1.0
    if len(target) >= 5 and (target in candidate or candidate in target): 
        return 1.0
    return difflib.SequenceMatcher(None, target, candidate).ratio()

# =====================================================================
# 3. DIRECT STREAM EXTRACTORS (INVOICES & ZOHO TRANSACTIONS)
# =====================================================================
def extract_invoice_metadata_from_stream(uploaded_file) -> Dict[str, Any]:
    """Reads supporting customer invoice streams to enrich missing names/IDs."""
    result = {"customer_name": None, "invoice_number": None}
    try:
        reader = PdfReader(uploaded_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        full_text_clean = " ".join(full_text.split())
        inv_num = str(uploaded_file.name).replace(".pdf", "").upper()
        inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = inv_match.group(1).upper() if inv_match else inv_num
        
        bill_to_match = re.search(r"Bill\s+To\s*(.*?)\s*Ship\s+To", full_text_clean, re.IGNORECASE | re.DOTALL)
        if bill_to_match:
            candidate = bill_to_match.group(1).strip()
            if len(candidate) > 2:
                result["customer_name"] = candidate
    except Exception:
        pass
    return result

def parse_zoho_summary_pdf_bulletproof(pdf_file) -> List[ZohoRecord]:
    """Strict row-by-row layout scanner that parses transactions without deduping."""
    records = []
    try:
        reader = PdfReader(pdf_file)
        row_counter = 0
        
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.split('\n'):
                line_clean = line.strip()
                if not line_clean:
                    continue
                
                # CRITICAL RULE: Explicitly block summary or total rows from ingestion
                if any(k in line_clean.lower() for k in ["payout summary", "total payout", "summary total", "statement total"]):
                    continue
                
                # Verify that the line contains trailing transaction numbers
                num_matches = list(re.finditer(r"[-+]?\$?\d+(?:,\d{3})*\.\d{2}", line_clean))
                if len(num_matches) < 3:
                    continue
                
                # Separate description strings cleanly from numeric blocks
                first_num_start = num_matches[0].start()
                text_part = line_clean[:first_num_start].strip()
                
                # Strip timestamps and localized file date markers
                text_part = re.sub(r'\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b', '', text_part)
                text_part = re.sub(r'\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b', '', text_part)
                text_part = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', '', text_part)
                text_part = re.sub(r'\s+', ' ', text_part).strip()
                
                # Look for an invoice number reference identifier
                inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", text_part, re.IGNORECASE)
                inv_id = inv_match.group(1).upper() if inv_match else None
                
                zoho_desc = text_part
                if inv_id:
                    zoho_desc = zoho_desc.replace(inv_match.group(0), "").strip()
                zoho_desc = re.sub(r'[^A-Za-z0-9\s\.\,\&\-]', '', zoho_desc).strip()
                
                # Extract numeric arrays safely
                vals = [clean_numeric_value(m.group(0)) for m in num_matches]
                raw_strs = [m.group(0) for m in num_matches]
                
                # FIX: Classify purely by negative indicators and string keywords instead of raw column counts
                is_refund_line = "refund" in line_clean.lower() or any("-" in s for s in raw_strs)
                
                row_counter += 1
                unique_tx_key = f"tx_{row_counter}_{datetime.now().strftime('%M%S')}"
                
                if is_refund_line:
                    records.append(ZohoRecord(
                        customer_name=zoho_desc if len(zoho_desc) > 1 else None,
                        gross_amount=0.0,
                        merchant_fee=0.0,
                        refund_amount=abs(vals[0]),
                        invoice_number=inv_id,
                        transaction_type="refund",
                        description=zoho_desc,
                        transaction_key=unique_tx_key
                    ))
                else:
                    records.append(ZohoRecord(
                        customer_name=zoho_desc if len(zoho_desc) > 1 else None,
                        gross_amount=abs(vals[0]),
                        merchant_fee=abs(vals[1]) if len(vals) > 1 else 0.0,
                        refund_amount=0.0,
                        invoice_number=inv_id,
                        transaction_type="payment",
                        description=zoho_desc,
                        transaction_key=unique_tx_key
                    ))
    except Exception as e:
        st.error(f"Critical execution error in row-based PDF parser engine: {e}")
    return records

# =====================================================================
# 4. STREAMLIT FRAMEWORK MATRIX
# =====================================================================
st.set_page_config(page_title="D365 General Journal Automation", layout="wide")
st.title("D365 General Journal Automation Engine")
st.subheader("Daily Operational Reconciliations Matrix (Rule Specification Version 2)")

possible_paths = ["Account Masterlist.xlsx", "Account Masterlist.csv"]
MASTERLIST_PATH = next((p for p in possible_paths if os.path.exists(p)), None)

if not MASTERLIST_PATH:
    st.error("❌ Baseline system reference file `Account Masterlist.xlsx` or `.csv` could not be located in workspace root.")
    st.stop()

st.sidebar.header("📅 Daily Variable Inputs")
boa_file = st.sidebar.file_uploader("1. Bank of America Report (Excel/CSV)", type=["xlsx", "csv"])
zoho_file = st.sidebar.file_uploader("2. Zoho Transaction Summary Sheet (PDF)", type=["pdf"])
uploaded_invoices = st.sidebar.file_uploader("3. Drop Bulk Supporting Customer Invoices Here (PDFs) [Optional]", type=["pdf"], accept_multiple_files=True)

if not (boa_file and zoho_file):
    st.info("💡 Staging status: Waiting for daily bank transactions and matching Zoho payout PDF documentation streams.")
else:
    # -----------------------------------------------------------------
    # INGESTION STEP A: CONSTRUCT REFERENCE REGISTRIES FROM FILES
    # -----------------------------------------------------------------
    browser_invoice_registry = {}
    if uploaded_invoices:
        for file_stream in uploaded_invoices:
            meta = extract_invoice_metadata_from_stream(file_stream)
            if meta["invoice_number"] and meta["customer_name"]:
                browser_invoice_registry[meta["invoice_number"]] = meta["customer_name"]

    form_master_lookup = {}
    form_paths = ["Form Master DB.xlsx", "Form Master DB.csv", "Form_Master_DB.xlsx", "Form_Master_DB.csv"]
    form_file = next((p for p in form_paths if os.path.exists(p)), None)
    if form_file:
        try:
            f_df = pd.read_csv(form_file) if form_file.endswith('.csv') else pd.read_excel(form_file)
            f_df.columns = [str(c).strip() for c in f_df.columns]
            term_col = f_df.columns[8] if len(f_df.columns) > 8 else None  # Dynamic map targeting column I
            for c in f_df.columns:
                if "invoice sent" in c.lower() or "term" in c.lower():
                    term_col = c
                    break
            name_col = next((c for c in f_df.columns if 'name' in c.lower() or 'customer' in c.lower()), f_df.columns[0])
            for _, r in f_df.iterrows():
                if pd.notna(r[name_col]):
                    form_master_lookup[normalize_name(str(r[name_col]))] = str(r[term_col]).strip().lower() if term_col and pd.notna(r[term_col]) else "due-on-receipt"
        except Exception:
            pass

    cash_code_master_lookup = {}
    cc_paths = ["Cash Code Masterlist.xlsx - Cash Code Masterlist.csv", "Cash Code Masterlist.xlsx", "Cash Code Masterlist.csv"]
    cc_file = next((p for p in cc_paths if os.path.exists(p)), None)
    if cc_file:
        try:
            cc_df = pd.read_csv(cc_file) if cc_file.endswith('.csv') else pd.read_excel(cc_file)
            cc_df.columns = [str(c).strip() for c in cc_df.columns]
            cc_code_col = next((c for c in cc_df.columns if 'code' in c.lower() and 'name' not in c.lower()), cc_df.columns[0])
            cc_name_col = next((c for c in cc_df.columns if 'name' in c.lower() or 'term' in c.lower()), cc_df.columns[1] if len(cc_df.columns) > 1 else cc_df.columns[0])
            for _, r in cc_df.iterrows():
                if pd.notna(r[cc_code_col]):
                    cash_code_master_lookup[str(r[cc_name_col]).strip().lower()] = str(r[cc_code_col]).strip()
        except Exception:
            pass

    master_df = pd.read_csv(MASTERLIST_PATH) if MASTERLIST_PATH.endswith('.csv') else pd.read_excel(MASTERLIST_PATH)
    master_df.columns = [str(col).strip() for col in master_df.columns]
    master_headers_lower = {str(col).lower(): str(col) for col in master_df.columns}
    
    ml_name_col = next((master_headers_lower[k] for k in ['account name', 'name', 'customer name'] if k in master_headers_lower), None)
    ml_num_col = next((master_headers_lower[k] for k in ['account #', 'account number', 'account no', 'account'] if k in master_headers_lower), None)
    ml_term_col = next((master_headers_lower[k] for k in ['payment term', 'payment terms', 'terms'] if k in master_headers_lower), None)
    ml_ticket_col = next((master_headers_lower[k] for k in ['cs/ps ticket', 'ticket', 'cs/ps'] if k in master_headers_lower), None)
    
    master_lookup: Dict[str, AccountMasterItem] = {}
    for _, row in master_df.iterrows():
        name_val = str(row[ml_name_col]).strip()
        num_val = str(row[ml_num_col]).strip()
        term_val = str(row.get(ml_term_col, 'due-on-receipt')).strip().lower() if ml_term_col else 'due-on-receipt'
        ticket_val = str(row.get(ml_ticket_col, '')).strip() if ml_ticket_col else ''
        
        master_lookup[name_val] = AccountMasterItem(
            account_number=num_val, account_name=name_val, payment_term=term_val,
            norm_name=normalize_name(name_val), norm_ticket=normalize_name(ticket_val)
        )

    # -----------------------------------------------------------------
    # INGESTION STEP B: PARSE BANK DATA ROWS
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
        
        if "ZOHO" in row_description.upper() and row_net_amount > 0:
            parsed_date = datetime.today().strftime('%m/%d/%Y')
            if date_target and pd.notna(row[date_target]):
                try:
                    parsed_date = pd.to_datetime(row[date_target]).strftime('%m/%d/%Y')
                except Exception:
                    pass
            boa_records.append(BOARecord(
                date=parsed_date, description=row_description, net_amount=row_net_amount,
                source_account=str(row.get(account_target, '')).strip() if account_target else "3371"
            ))

    # Trigger row extraction
    zoho_records = parse_zoho_summary_pdf_bulletproof(zoho_file)

    # =====================================================================
    # 5. TRANSACTION RESOLUTION LOOP & LEDGER WRITING
    # =====================================================================
    all_journal_lines = []
    diagnostic_logs = []
    batch_validation_failed = False

    for boa_rec in boa_records:
        if not zoho_records:
            continue

        # BALANCING VALIDATION MATRICES
        total_gross = sum(z.gross_amount for z in zoho_records)
        total_fees = sum(z.merchant_fee for z in zoho_records)
        total_refunds = sum(z.refund_amount for z in zoho_records)
        calculated_net = round(total_gross - total_fees - total_refunds, 2)
        
        if calculated_net != round(boa_rec.net_amount, 2):
            st.error(f"🚨 **Mathematical Balance Mismatch!** Total Gross ({total_gross}) - Fees ({total_fees}) - Refunds ({total_refunds}) = {calculated_net}. Expected Bank Net: {boa_rec.net_amount}.")
            batch_validation_failed = True

        offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "B1000002")
        resolved_payments_for_fee_desc = []
        
        for z_rec in zoho_records:
            resolved_name = None
            
            # Step 1: Row name target
            if z_rec.customer_name and len(z_rec.customer_name) > 1:
                resolved_name = z_rec.customer_name
            
            # Step 2: Fall back to business name in description if no invoice number
            if not resolved_name and not z_rec.invoice_number and z_rec.description:
                resolved_name = z_rec.description
                
            # Step 3: Enrich name from uploaded invoice PDF using invoice number
            if (not resolved_name or resolved_name == "Unspecified Payment Entry") and z_rec.invoice_number:
                if z_rec.invoice_number in browser_invoice_registry:
                    resolved_name = browser_invoice_registry[z_rec.invoice_number]

            norm_target = normalize_name(resolved_name) if resolved_name else ""
            
            matched_master_item = None
            best_score = 0.0
            best_candidate = "No Close Matches"
            
            # Exact lookup priority check
            for item in master_lookup.values():
                if norm_target and norm_target == item.norm_name:
                    matched_master_item = item
                    break
            
            # Fuzzy fallback tracking loop
            if not matched_master_item and norm_target:
                for item in master_lookup.values():
                    s1 = get_match_score(norm_target, item.norm_name)
                    s2 = get_match_score(norm_target, item.norm_ticket) if item.norm_ticket else 0.0
                    highest_sim_score = max(s1, s2)
                    
                    if highest_sim_score > best_score:
                        best_score = highest_sim_score
                        best_candidate = item.account_name
                    
                    if highest_sim_score >= 0.85: 
                        matched_master_item = item
                        break

            if not matched_master_item:
                account_num = "21040102-B1000002"
                account_type = "Ledger"
                account_name = "Temporary Receipt"
                cash_code = "AR012"
                desc = f"{resolved_name if resolved_name else 'Unspecified Row'} (UNRECORDED ENTITY)_{boa_rec.description}"
                
                diagnostic_logs.append({
                    "Invoice ID": z_rec.invoice_number if z_rec.invoice_number else "MISSING",
                    "Extracted Target Label": resolved_name,
                    "Normalized Flag": norm_target,
                    "Fuzzy Evaluation Match": f"{best_candidate} ({round(best_score * 100, 1)}%)"
                })
            else:
                master_item = matched_master_item
                account_num = master_item.account_number
                account_type = "Customer"
                account_name = master_item.account_name
                
                lookup_key = normalize_name(account_name)
                resolved_term = form_master_lookup.get(lookup_key, master_item.payment_term)
                
                if resolved_term in cash_code_master_lookup:
                    cash_code = cash_code_master_lookup[resolved_term]
                else:
                    cash_code = CASH_CODE_MAPPING.get(resolved_term, "AR012")
                    
                prefix = "MPP " if cash_code == "AR002" else ""
                desc = f"{prefix}{account_num} {account_name}_{boa_rec.description}"
                
                if z_rec.transaction_type == "payment":
                    resolved_payments_for_fee_desc.append(f"{account_num} {account_name}")

            # Append Lines
            if z_rec.transaction_type == "payment":
                all_journal_lines.append({
                    "Date": boa_rec.date, "Voucher": "", "Account name": account_name,
                    "Company": "bwa", "Account type": account_type, "Account": account_num,
                    "Posting Profile": "AutoPost" if account_type == "Customer" else "", "Cash code": cash_code, "Description": desc,
                    "Debit": "", "Credit": z_rec.gross_amount, "Item sales tax group": "", "Sales tax code": "",
                    "Offset company": "bwa", "Bank Account Type": "Bank", "Offset account": offset_acct,
                    "Offset transaction text": "", "Currency": "USD", "Exchange rate": 1.00,
                    "Item sales tax group2": "", "Sales group": "AVATAX", "Withholding tax group": "",
                    "Release date": "", "Reversing entry": "No", "Reversing date": ""
                })
            
            elif z_rec.transaction_type == "refund":
                refund_desc = f"Refund Line Item Adjustment {account_num} {account_name}_{boa_rec.description}"
                all_journal_lines.append({
                    "Date": boa_rec.date, "Voucher": "", "Account name": account_name,
                    "Company": "bwa", "Account type": account_type, "Account": account_num,
                    "Posting Profile": "AutoPost" if account_type == "Customer" else "", "Cash code": cash_code, "Description": refund_desc,
                    "Debit": z_rec.refund_amount, "Credit": "", "Item sales tax group": "", "Sales tax code": "",
                    "Offset company": "bwa", "Bank Account Type": "Bank", "Offset account": offset_acct,
                    "Offset transaction text": "", "Currency": "USD", "Exchange rate": 1.00,
                    "Item sales tax group2": "", "Sales group": "AVATAX", "Withholding tax group": "",
                    "Release date": "", "Reversing entry": "No", "Reversing date": ""
                })

        # Append Grouped Processing Fees
        if total_fees > 0 and not batch_validation_failed:
            if resolved_payments_for_fee_desc:
                concatenated_entities = ", ".join(list(set(resolved_payments_for_fee_desc)))
                fee_desc = f"Zoho Merchant Fee_{concatenated_entities}"
            else:
                fee_desc = f"Zoho Merchant Fee_Unresolved Batch Suspense Pool"
                
            all_journal_lines.append({
                "Date": boa_rec.date, "Voucher": "", "Account name": "Outside Service (Finance)",
                "Company": "bwa", "Account type": "Ledger", "Account": "43170111-U26C05001-B735350-UOA003",
                "Posting Profile": "", "Cash code": "OSF005", "Description": fee_desc,
                "Debit": total_fees, "Credit": "", "Item sales tax group": "", "Sales tax code": "",
                "Offset company": "bwa", "Bank Account Type": "Bank", "Offset account": offset_acct,
                "Offset transaction text": "", "Currency": "USD", "Exchange rate": 1.00,
                "Item sales tax group2": "", "Sales group": "AVATAX", "Withholding tax group": "",
                "Release date": "", "Reversing entry": "No", "Reversing date": ""
            })

    # Render results
    if all_journal_lines and not batch_validation_failed:
        st.success(f"### Verification Successful: {len(all_journal_lines)} Balanced Journal Lines Generated.")
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
    elif batch_validation_failed:
        st.error("❌ **Processing Halted:** Mathematical balance verification failed. Please check the PDF contents against the bank deposit row data metrics.")
        
    if diagnostic_logs:
        st.markdown("---")
        with st.expander("🚨 Unrecorded Description Review Dashboard (Preserved Rows)", expanded=True):
            st.warning("These transactions were safely isolated and assigned to the standard suspense clearing ledger.")
            st.dataframe(pd.DataFrame(diagnostic_logs))
