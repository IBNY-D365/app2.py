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
import tempfile

from mappings import CASH_CODE_MAPPING, OFFSET_ACCOUNT_ROUTING, D365_TEMPLATE_COLUMNS
from core.models import BOARecord, ZohoRecord, AccountMasterItem
from parsers.invoice_parser import extract_invoice_metadata_intelligent, parse_zoho_summary_pdf_bulletproof
from core.validators import normalize_name, get_match_score


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def money_to_float(val: Any) -> float:
    if pd.isna(val) or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip().replace("$", "").replace(",", "")
    s = s.replace("−", "-").replace("–", "-")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]

    try:
        return float(s)
    except ValueError:
        return 0.0


def norm_key(text: Any) -> str:
    return normalize_name(str(text)) if text is not None and not pd.isna(text) else ""


def load_master_lookup(master_df: pd.DataFrame) -> Dict[str, AccountMasterItem]:
    master_df.columns = [str(c).strip() for c in master_df.columns]
    headers = {c.lower(): c for c in master_df.columns}

    name_col = next((headers[k] for k in ["account name", "name", "customer name"] if k in headers), None)
    num_col = next((headers[k] for k in ["account #", "account number", "account no", "account"] if k in headers), None)
    term_col = next((headers[k] for k in ["payment term", "payment terms", "terms"] if k in headers), None)
    ticket_col = next((headers[k] for k in ["cs/ps ticket", "ticket", "cs/ps"] if k in headers), None)

    if not name_col or not num_col:
        raise ValueError("Could not locate Account Name / Account # columns in Account Masterlist.")

    lookup: Dict[str, AccountMasterItem] = {}
    for _, row in master_df.iterrows():
        name_val = str(row[name_col]).strip()
        num_val = str(row[num_col]).strip()
        if not name_val or name_val.lower() == "nan":
            continue
        if not num_val or num_val.lower() == "nan":
            continue

        term_val = str(row.get(term_col, "due-on-receipt")).strip().lower() if term_col else "due-on-receipt"
        ticket_val = str(row.get(ticket_col, "")).strip() if ticket_col else ""

        lookup[name_val] = AccountMasterItem(
            account_number=num_val,
            account_name=name_val,
            payment_term=term_val,
            norm_name=normalize_name(name_val),
            norm_ticket=normalize_name(ticket_val),
        )
    return lookup


def load_form_master_terms(form_master_path: str) -> Dict[str, str]:
    """
    Column I = Invoice Sent.
    Returns account-name -> payment-term mapping.
    """
    df = pd.read_excel(form_master_path)
    df.columns = [str(c).strip() for c in df.columns]

    # Use column I by position if headers are unreliable.
    invoice_sent_col = df.columns[8] if len(df.columns) >= 9 else None

    # Try to locate an account/customer name column.
    name_col = next(
        (c for c in df.columns if "account" in c.lower() or "customer" in c.lower() or "name" in c.lower()),
        None
    )

    if not name_col or not invoice_sent_col:
        return {}

    out = {}
    for _, row in df.iterrows():
        acct = str(row.get(name_col, "")).strip()
        term = str(row.get(invoice_sent_col, "")).strip().lower()
        if acct and acct.lower() != "nan":
            out[normalize_name(acct)] = term
    return out


def load_cash_code_map(cash_master_path: str) -> Dict[str, str]:
    """
    Builds payment-term -> cash-code map from Cash Code Masterlist.
    Adjust column names here if your workbook differs.
    """
    df = pd.read_excel(cash_master_path)
    df.columns = [str(c).strip() for c in df.columns]
    headers = {c.lower(): c for c in df.columns}

    term_col = next((headers[k] for k in ["payment term", "payment terms", "terms"] if k in headers), None)
    code_col = next((headers[k] for k in ["cash code", "cash_code", "code"] if k in headers), None)

    if not term_col or not code_col:
        return {}

    mapping = {}
    for _, row in df.iterrows():
        term = str(row.get(term_col, "")).strip().lower()
        code = str(row.get(code_col, "")).strip()
        if term and code:
            mapping[term] = code
    return mapping


def resolve_master_item(name_or_desc: str, master_lookup: Dict[str, AccountMasterItem]) -> Optional[AccountMasterItem]:
    target = normalize_name(name_or_desc)
    if not target:
        return None

    best_item = None
    best_score = 0.0

    for item in master_lookup.values():
        s1 = get_match_score(target, item.norm_name)
        s2 = get_match_score(target, item.norm_ticket) if item.norm_ticket else 0.0
        score = max(s1, s2)

        if score > best_score:
            best_score = score
            best_item = item

    return best_item if best_score >= 0.80 else None


def get_cash_code_for_term(term: str, cash_term_map: Dict[str, str]) -> str:
    term = (term or "").strip().lower()
    if term in cash_term_map:
        return cash_term_map[term]
    return CASH_CODE_MAPPING["fallback"][0]


# --------------------------------------------------
# Main flow
# --------------------------------------------------
st.set_page_config(page_title="D365 General Journal Automation", layout="wide")
st.title("D365 General Journal Automation Engine")
st.subheader("Daily Operational Reconciliations Matrix")

masterlist_path = next((p for p in ["Account Masterlist.xlsx", "Account Masterlist.csv"] if os.path.exists(p)), None)
form_master_path = next((p for p in ["Form Master DB.xlsx"] if os.path.exists(p)), None)
cash_master_path = next((p for p in ["Cash Code Masterlist.xlsx"] if os.path.exists(p)), None)

if not masterlist_path:
    st.error("Missing Account Masterlist.")
    st.stop()

boa_file = st.sidebar.file_uploader("1. Bank of America Report (Excel/CSV)", type=["xlsx", "csv"])
zoho_file = st.sidebar.file_uploader("2. Zoho Transaction Summary or Direct Invoices (PDF/Excel/CSV)", type=["pdf", "xlsx", "csv"])
uploaded_invoices = st.sidebar.file_uploader("3. Extra Customer Invoices (PDFs) [Optional]", type=["pdf"], accept_multiple_files=True)

if not (boa_file and zoho_file):
    st.info("Upload BOA and Zoho files to continue.")
    st.stop()

# Master data
master_df = pd.read_excel(masterlist_path) if masterlist_path.endswith(".xlsx") else pd.read_csv(masterlist_path)
master_lookup = load_master_lookup(master_df)
form_terms = load_form_master_terms(form_master_path) if form_master_path else {}
cash_term_map = load_cash_code_map(cash_master_path) if cash_master_path else {}

# Parse uploaded invoice PDFs
invoice_cache = {}
invoice_sources_list: List[ZohoRecord] = []
if uploaded_invoices:
    for inv in uploaded_invoices:
        meta = extract_invoice_metadata_intelligent(inv)
        inv_no = str(meta.get("invoice_number") or "").strip()
        if inv_no:
            invoice_cache[inv_no] = meta
            invoice_sources_list.append(
                ZohoRecord(
                    customer_name=meta.get("customer_name"),
                    gross_amount=money_to_float(meta.get("gross_amount", 0.0)),
                    merchant_fee=0.0,
                    refund_amount=0.0,
                    invoice_number=inv_no,
                    fallback_personal_name=meta.get("fallback_personal_name"),
                    transaction_type="payment",
                )
            )

# Parse BOA
if boa_file.name.lower().endswith(".csv"):
    boa_df = pd.read_csv(boa_file)
else:
    boa_df = pd.read_excel(boa_file)

boa_df.columns = [str(c).strip().lower() for c in boa_df.columns]
desc_col = next((c for c in ["description", "transaction description", "memo", "payee"] if c in boa_df.columns), None)
amt_col = next((c for c in ["net amount", "amount", "net_amount"] if c in boa_df.columns), None)
date_col = next((c for c in ["posting date", "date", "transaction date"] if c in boa_df.columns), None)
acct_col = next((c for c in ["source account", "account", "account number", "account_number"] if c in boa_df.columns), None)

boa_records: List[BOARecord] = []
for _, row in boa_df.iterrows():
    desc = str(row.get(desc_col, ""))
    amt = money_to_float(row.get(amt_col, 0.0))
    if "ZOHO PAYMENTS" in desc.upper() and amt > 0:
        dt = datetime.today().date()
        if date_col and pd.notna(row.get(date_col)):
            try:
                dt = pd.to_datetime(row.get(date_col)).date()
            except Exception:
                pass
        boa_records.append(
            BOARecord(
                date=dt,
                description=desc,
                net_amount=amt,
                source_account=str(row.get(acct_col, "3371")).strip(),
            )
        )

# Parse Zoho
raw_zoho_pool: List[ZohoRecord] = []
if zoho_file.name.lower().endswith(".pdf"):
    raw_zoho_pool = parse_zoho_summary_pdf_bulletproof(zoho_file)
else:
    tmp_suffix = os.path.splitext(zoho_file.name)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=tmp_suffix) as tmp:
        tmp.write(zoho_file.getbuffer())
        temp_path = tmp.name
    try:
        # If you already have a Zoho CSV/XLSX parser, call it here.
        # Otherwise this block can be adapted to read Excel/CSV directly.
        df = pd.read_excel(temp_path) if temp_path.lower().endswith(".xlsx") else pd.read_csv(temp_path)
        df.columns = [str(c).strip() for c in df.columns]

        cust_col = next((c for c in df.columns if "customer" in c.lower()), None)
        gross_col = next((c for c in df.columns if "gross" in c.lower() or ("amount" in c.lower() and "net" not in c.lower() and "fee" not in c.lower())), None)
        fee_col = next((c for c in df.columns if "fee" in c.lower()), None)
        inv_col = next((c for c in df.columns if "invoice" in c.lower()), None)
        desc_col2 = next((c for c in df.columns if "description" in c.lower()), None)

        for _, row in df.iterrows():
            gross = money_to_float(row.get(gross_col, 0.0))
            fee = abs(money_to_float(row.get(fee_col, 0.0)))
            inv_no = str(row.get(inv_col, "")).strip() if inv_col and pd.notna(row.get(inv_col)) else None
            c_name = str(row.get(cust_col, "")).strip() if cust_col and pd.notna(row.get(cust_col)) else None
            desc = str(row.get(desc_col2, "")).strip() if desc_col2 and pd.notna(row.get(desc_col2)) else None

            raw_zoho_pool.append(
                ZohoRecord(
                    customer_name=c_name,
                    gross_amount=gross,
                    merchant_fee=fee,
                    refund_amount=0.0,
                    invoice_number=inv_no,
                    fallback_personal_name=None,
                    transaction_type="payment",
                )
            )
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

# Merge invoice PDFs into Zoho payment rows by invoice number first,
# then by exact gross amount if invoice number is missing.
payment_records: List[ZohoRecord] = []
refund_records: List[ZohoRecord] = []

for r in raw_zoho_pool:
    if getattr(r, "transaction_type", "payment") == "refund":
        refund_records.append(r)
    else:
        payment_records.append(r)

# Enrich payments using invoice PDFs
for inv_rec in invoice_sources_list:
    matched = None

    if inv_rec.invoice_number:
        for p in payment_records:
            if p.invoice_number and p.invoice_number.upper() == inv_rec.invoice_number.upper():
                matched = p
                break

    if not matched:
        # Exact gross match fallback for rows that do not expose invoice numbers
        candidates = [p for p in payment_records if abs(p.gross_amount - inv_rec.gross_amount) <= 0.01]
        if len(candidates) == 1:
            matched = candidates[0]

    if matched:
        if not matched.customer_name and inv_rec.customer_name:
            matched.customer_name = inv_rec.customer_name
        if not matched.fallback_personal_name and inv_rec.fallback_personal_name:
            matched.fallback_personal_name = inv_rec.fallback_personal_name
        if not matched.invoice_number:
            matched.invoice_number = inv_rec.invoice_number

# Resolve no-invoice rows using Zoho description/business name
for p in payment_records:
    if not p.customer_name:
        candidate_name = p.fallback_personal_name or p.customer_name
        if not candidate_name and p.description:
            candidate_name = p.description
        if candidate_name:
            master_item = resolve_master_item(candidate_name, master_lookup)
            if master_item:
                p.customer_name = master_item.account_name

zoho_records = payment_records + refund_records

# Reconcile and build journal
all_journal_lines = []
validation_errors = []

for boa_rec in boa_records:
    gross_total = round(sum(x.gross_amount for x in payment_records), 2)
    fee_total = round(sum(abs(x.merchant_fee) for x in payment_records), 2)
    refund_total = round(sum(abs(x.refund_amount) for x in refund_records), 2)
    calculated_net = round(gross_total - fee_total - refund_total, 2)

    if abs(calculated_net - boa_rec.net_amount) > 0.01:
        validation_errors.append(
            f"Reconciliation mismatch: gross={gross_total}, fee={fee_total}, refund={refund_total}, calc_net={calculated_net}, boa_net={boa_rec.net_amount}"
        )
        continue

    offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "B1000002")

    for p in payment_records:
        master_item = resolve_master_item(p.customer_name or p.fallback_personal_name or "", master_lookup)

        if master_item:
            account_num = master_item.account_number
            account_name = master_item.account_name
            payment_term = form_terms.get(normalize_name(account_name), master_item.payment_term)
            cash_code = get_cash_code_for_term(payment_term, cash_term_map)
            account_type = "Customer"
            posting_profile = "AutoPost"
        else:
            account_num = "21040102-B1000002"
            account_name = "Temporary Receipt"
            cash_code = "AR012"
            account_type = "Ledger"
            posting_profile = ""

        all_journal_lines.append({
            "Date": boa_rec.date.strftime("%m/%d/%Y"),
            "Voucher": "",
            "Account name": account_name,
            "Company": "bwa",
            "Account type": account_type,
            "Account": account_num,
            "Posting Profile": posting_profile,
            "Cash code": cash_code,
            "Description": f"{account_num} {account_name}_{boa_rec.description}",
            "Debit": "",
            "Credit": p.gross_amount,
            "Item sales tax group": "",
            "Sales tax code": "",
            "Offset company": "bwa",
            "Bank Account Type": "Bank",
            "Offset account": offset_acct,
            "Offset transaction text": "",
            "Currency": "USD",
            "Exchange rate": 1.00,
            "Item sales tax group2": "",
            "Sales tax group": "AVATAX",
            "Withholding tax group": "",
            "Release date": "",
            "Reversing entry": "No",
            "Reversing date": ""
        })

    if fee_total > 0:
        all_journal_lines.append({
            "Date": boa_rec.date.strftime("%m/%d/%Y"),
            "Voucher": "",
            "Account name": "Outside Service (Finance)",
            "Company": "bwa",
            "Account type": "Ledger",
            "Account": "43170111-U26C05001-B735350-UOA003",
            "Posting Profile": "",
            "Cash code": "OSF005",
            "Description": f"Zoho Merchant Fee_{boa_rec.description}",
            "Debit": fee_total,
            "Credit": "",
            "Item sales tax group": "",
            "Sales tax code": "",
            "Offset company": "bwa",
            "Bank Account Type": "Bank",
            "Offset account": offset_acct,
            "Offset transaction text": "",
            "Currency": "USD",
            "Exchange rate": 1.00,
            "Item sales tax group2": "",
            "Sales tax group": "AVATAX",
            "Withholding tax group": "",
            "Release date": "",
            "Reversing entry": "No",
            "Reversing date": ""
        })

    if refund_total > 0:
        all_journal_lines.append({
            "Date": boa_rec.date.strftime("%m/%d/%Y"),
            "Voucher": "",
            "Account name": "Refund Clearing",
            "Company": "bwa",
            "Account type": "Ledger",
            "Account": "REFUND-CLEARING-ACCOUNT",
            "Posting Profile": "",
            "Cash code": "OSF005",
            "Description": f"Zoho Refunds_{boa_rec.description}",
            "Debit": refund_total,
            "Credit": "",
            "Item sales tax group": "",
            "Sales tax code": "",
            "Offset company": "bwa",
            "Bank Account Type": "Bank",
            "Offset account": offset_acct,
            "Offset transaction text": "",
            "Currency": "USD",
            "Exchange rate": 1.00,
            "Item sales tax group2": "",
            "Sales tax group": "AVATAX",
            "Withholding tax group": "",
            "Release date": "",
            "Reversing entry": "No",
            "Reversing date": ""
        })

if validation_errors:
    st.error("### Pipeline Validation Discrepancies Checked")
    for e in validation_errors:
        st.markdown(e)

if all_journal_lines:
    st.success(f"### Transformed {len(all_journal_lines)} Journal Lines Successfully!")
    output_df = pd.DataFrame(all_journal_lines, columns=D365_TEMPLATE_COLUMNS)
    st.dataframe(output_df)
