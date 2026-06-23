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
    "fallback": ("AR012", "AR Collection_Other"),
}

OFFSET_ACCOUNT_ROUTING = {
    "3371": "B1000002",
    "3924": "B1000003",
    "3384": "B1000001",
}

# Replace this with the actual D365 refund ledger account.
REFUND_CLEARING_ACCOUNT = "REFUND-CLEARING-ACCOUNT"

D365_TEMPLATE_COLUMNS = [
    "Date", "Voucher", "Account name", "Company", "Account type", "Account",
    "Posting Profile", "Cash code", "Description", "Debit", "Credit",
    "Item sales tax group", "Sales tax code", "Offset company", "Bank Account Type",
    "Offset account", "Offset transaction text", "Currency", "Exchange rate",
    "Item sales tax group2", "Sales tax group", "Withholding tax group",
    "Release date", "Reversing entry", "Reversing date",
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
    gross_amount: float = 0.0
    merchant_fee: float = 0.0
    refund_amount: float = 0.0
    invoice_number: Optional[str] = None
    fallback_personal_name: Optional[str] = None
    transaction_type: str = "payment"  # payment | refund


class AccountMasterItem(BaseModel):
    account_number: str
    account_name: str
    payment_term: str
    norm_name: str
    norm_ticket: str


MONEY_TOKEN_PATTERN = re.compile(r"\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?")


def clean_numeric_value(val: Any) -> float:
    """Converts standard and accounting-style money values to floats."""
    if pd.isna(val) or val is None:
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    raw = str(val).strip()
    if raw == "":
        return 0.0

    is_negative = False
    raw = raw.replace("−", "-")

    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]

    if raw.startswith("-") or raw.endswith("-"):
        is_negative = True

    cleaned_str = (
        raw.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("+", "")
        .replace("-", "")
        .strip()
    )

    try:
        number = float(cleaned_str)
    except ValueError:
        return 0.0

    return -abs(number) if is_negative else number


def normalize_invoice_number(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip().upper()
    if not text or text.lower() == "nan":
        return None

    text = re.sub(r"\.0$", "", text)
    return text


def normalize_name(name: str) -> str:
    """Removes common suffixes and strips all non-alphanumeric characters."""
    if not name or pd.isna(name):
        return ""

    n = str(name).lower()
    n = re.sub(r"\b(inc|llc|corp|ltd|incorporated|company|co|pllc)\b", "", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n


def get_match_score(target: str, candidate: str) -> float:
    """Calculates a similarity score between two normalized names."""
    if not target or not candidate:
        return 0.0

    if target == candidate:
        return 1.0

    if len(target) >= 5 and (target in candidate or candidate in target):
        return 1.0

    return difflib.SequenceMatcher(None, target, candidate).ratio()


def read_pdf_text(pdf_file) -> str:
    """Reads text from an uploaded PDF and resets file pointer when possible."""
    try:
        pdf_file.seek(0)
    except Exception:
        pass

    reader = PdfReader(pdf_file)
    full_text = ""

    for page in reader.pages:
        full_text += page.extract_text() or ""

    try:
        pdf_file.seek(0)
    except Exception:
        pass

    return full_text


def make_journal_line(
    boa_rec: BOARecord,
    account_name: str,
    account_type: str,
    account: str,
    posting_profile: str,
    cash_code: str,
    description: str,
    debit: Any,
    credit: Any,
    offset_acct: str,
) -> Dict[str, Any]:
    return {
        "Date": boa_rec.date,
        "Voucher": "",
        "Account name": account_name,
        "Company": "bwa",
        "Account type": account_type,
        "Account": account,
        "Posting Profile": posting_profile,
        "Cash code": cash_code,
        "Description": description,
        "Debit": debit,
        "Credit": credit,
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
        "Reversing date": "",
    }

# =====================================================================
# 3. ADVANCED EXTRACTION ENGINE
# =====================================================================
def extract_invoice_metadata_intelligent(pdf_file) -> Dict[str, Any]:
    """Scans the invoice to capture the precise Paid Amount and business entity."""
    result = {
        "customer_name": None,
        "invoice_number": None,
        "gross_amount": 0.0,
        "fallback_personal_name": None,
    }

    try:
        full_text = read_pdf_text(pdf_file)
        full_text_clean = " ".join(full_text.split())

        inv_num = pdf_file.name.replace(".pdf", "")
        inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = normalize_invoice_number(inv_match.group(1)) if inv_match else normalize_invoice_number(inv_num)

        pm_match = re.search(
            r"Payment\s*Made[^\d\$]*\$?([0-9,]+\.\d{2})",
            full_text_clean,
            re.IGNORECASE,
        )

        if pm_match:
            result["gross_amount"] = clean_numeric_value(pm_match.group(1))
        else:
            totals = re.findall(
                r"Total[^\d\$]*\$?([0-9,]+\.\d{2})",
                full_text_clean,
                re.IGNORECASE,
            )

            if totals:
                result["gross_amount"] = clean_numeric_value(totals[-1])
            else:
                all_decimals = [
                    clean_numeric_value(n)
                    for n in re.findall(r"\b\d+(?:,\d{3})*\.\d{2}\b", full_text_clean)
                ]
                result["gross_amount"] = max(all_decimals) if all_decimals else 0.0

        cust_match = re.search(
            r"Customer\s*Name[\s\:]*([A-Za-z0-9\s\.\,\&\-]+?)(?:\s+(?:Invoice|Date|Amount|Terms|Bill\s*To|Ship\s*To|$))",
            full_text_clean,
            re.IGNORECASE,
        )

        if cust_match:
            result["customer_name"] = cust_match.group(1).strip()

        # Important: this does not stop at a digit, so names like 301 E 57th St Gym LLC work.
        bill_to_match = re.search(
            r"Bill\s+To\s*(.*?)\s*Ship\s+To",
            full_text_clean,
            re.IGNORECASE | re.DOTALL,
        )

        if bill_to_match:
            candidate = bill_to_match.group(1).strip()
            result["fallback_personal_name"] = candidate

            if not result["customer_name"]:
                result["customer_name"] = candidate

        if not result["customer_name"]:
            biz_matches = re.findall(
                r"InBody\d*\s*-\s*[^-]+?\-\s*([A-Za-z0-9\s\.\,\&]+)",
                full_text_clean,
                re.IGNORECASE,
            )

            for match in biz_matches:
                candidate = re.sub(r"\d+\.\d{2}.*", "", match).strip()
                if candidate and not any(
                    k in candidate.lower()
                    for k in ["malfunction", "check required", "sku", "labor", "board", "cable", "loaner"]
                ):
                    result["customer_name"] = candidate
                    break

    except Exception as e:
        st.error(f"Error executing intelligent metadata capture: {e}")

    return result


def extract_refund_records_from_zoho_text(full_text: str) -> List[ZohoRecord]:
    """Extracts payout-level refunds separately from merchant fees."""
    records: List[ZohoRecord] = []
    flat_text = re.sub(r"\s+", " ", full_text)

    # Zoho payout summary commonly looks like:
    # Refunds 1 -$43.54 $0.00 -$43.54
    refund_match = re.search(
        r"\bRefunds?\b\s+\d+\s+((?:\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?\s*){1,4})",
        flat_text,
        re.IGNORECASE,
    )

    refund_text = refund_match.group(0) if refund_match else ""

    if not refund_text:
        # Fallback: capture a small Refunds section.
        section_match = re.search(
            r"\bRefunds?\b(.{0,500}?)(?:\bPayments\b|\bTransactions\b|\bTotal\b|$)",
            flat_text,
            re.IGNORECASE,
        )
        refund_text = section_match.group(0) if section_match else ""

    if refund_text:
        tokens = MONEY_TOKEN_PATTERN.findall(refund_text)
        values = [clean_numeric_value(token) for token in tokens]
        negative_values = [abs(v) for v in values if v < 0]
        nonzero_values = [abs(v) for v in values if abs(v) > 0]

        refund_amount = 0.0

        if negative_values:
            # Do not sum both gross and total columns; the first negative is the refund gross.
            refund_amount = negative_values[0]
        elif nonzero_values:
            refund_amount = nonzero_values[0]

        if refund_amount > 0:
            records.append(
                ZohoRecord(
                    customer_name="Zoho Refund",
                    gross_amount=0.0,
                    merchant_fee=0.0,
                    refund_amount=refund_amount,
                    invoice_number=None,
                    fallback_personal_name=None,
                    transaction_type="refund",
                )
            )

    return records


def parse_zoho_summary_pdf_bulletproof(pdf_file) -> List[ZohoRecord]:
    """Parses Zoho PDF payout payments and refunds as separate transaction records."""
    records: List[ZohoRecord] = []

    try:
        full_text = read_pdf_text(pdf_file)
        flat_text = re.sub(r"\s+", " ", full_text)

        # First: parse invoice/payment lines.
        for line in full_text.split("\n"):
            if "INV-" not in line.upper():
                continue

            inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", line, re.IGNORECASE)
            if not inv_match:
                continue

            inv_id = normalize_invoice_number(inv_match.group(1))
            after_inv_text = line[inv_match.end():]
            amounts = MONEY_TOKEN_PATTERN.findall(after_inv_text)

            if not amounts:
                continue

            amount_values = [abs(clean_numeric_value(a)) for a in amounts]

            # Typical line: gross, fee, net.
            # Gross is usually max. Fee is the smallest positive non-zero amount.
            gross = max(amount_values)
            nonzero_amounts = [v for v in amount_values if v > 0]
            fee = min(nonzero_amounts) if len(nonzero_amounts) >= 2 else 0.0

            name_candidate = after_inv_text
            for amt in amounts:
                name_candidate = name_candidate.replace(amt, "")

            cust_name = name_candidate.replace("$", "").replace(",", "").strip()
            cust_name = re.sub(r"\s+", " ", cust_name)

            if gross > 0 and not any(r.invoice_number == inv_id and r.transaction_type == "payment" for r in records):
                records.append(
                    ZohoRecord(
                        customer_name=cust_name if cust_name else None,
                        gross_amount=gross,
                        merchant_fee=abs(fee),
                        refund_amount=0.0,
                        invoice_number=inv_id,
                        fallback_personal_name=None,
                        transaction_type="payment",
                    )
                )

        # If line-by-line extraction failed, try a flat-text invoice pattern.
        if not any(r.transaction_type == "payment" for r in records):
            pattern = re.compile(
                r"(INV-[A-Za-z0-9\-]+)\s+(.{0,150}?)\s+"
                r"(\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?)"
                r"(?:\s+(\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?))?"
                r"(?:\s+(\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?))?",
                re.IGNORECASE,
            )

            for match in pattern.finditer(flat_text):
                inv_id = normalize_invoice_number(match.group(1))
                raw_name = re.sub(r"\s+", " ", match.group(2)).strip()

                amount_tokens = [g for g in match.groups()[2:] if g]
                values = [abs(clean_numeric_value(token)) for token in amount_tokens]

                if not values:
                    continue

                gross = max(values)
                nonzero_values = [v for v in values if v > 0]
                fee = min(nonzero_values) if len(nonzero_values) >= 2 else 0.0

                if gross > 0 and not any(r.invoice_number == inv_id and r.transaction_type == "payment" for r in records):
                    records.append(
                        ZohoRecord(
                            customer_name=raw_name if len(raw_name) > 3 else None,
                            gross_amount=gross,
                            merchant_fee=abs(fee),
                            refund_amount=0.0,
                            invoice_number=inv_id,
                            fallback_personal_name=None,
                            transaction_type="payment",
                        )
                    )

        # Second: parse refunds separately.
        existing_refund_total = sum(r.refund_amount for r in records if r.transaction_type == "refund")
        if existing_refund_total == 0:
            records.extend(extract_refund_records_from_zoho_text(full_text))

    except Exception as e:
        st.error(f"Error executing summary parser: {e}")

    return records


def parse_zoho_excel_or_csv(zoho_file) -> List[ZohoRecord]:
    """Parses Zoho Excel/CSV and separates payments from refunds."""
    records: List[ZohoRecord] = []

    if zoho_file.name.lower().endswith(".csv"):
        zoho_df = pd.read_csv(zoho_file)
    else:
        zoho_df = pd.read_excel(zoho_file)

    zoho_df.columns = [str(c).strip() for c in zoho_df.columns]

    cust_col = next((c for c in zoho_df.columns if "customer" in c.lower()), None)
    gross_col = next(
        (
            c
            for c in zoho_df.columns
            if "gross" in c.lower()
            or ("amount" in c.lower() and "net" not in c.lower() and "fee" not in c.lower())
        ),
        None,
    )
    fee_col = next((c for c in zoho_df.columns if "fee" in c.lower()), None)
    inv_col = next((c for c in zoho_df.columns if "invoice" in c.lower()), None)
    type_col = next((c for c in zoho_df.columns if "type" in c.lower() or "transaction" in c.lower()), None)

    for _, row in zoho_df.iterrows():
        c_name = str(row[cust_col]).strip() if cust_col and pd.notna(row[cust_col]) else None
        inv = normalize_invoice_number(row[inv_col]) if inv_col and pd.notna(row[inv_col]) else None
        gross = clean_numeric_value(row[gross_col]) if gross_col else 0.0
        fee = abs(clean_numeric_value(row[fee_col])) if fee_col else 0.0
        transaction_label = str(row[type_col]).strip().lower() if type_col and pd.notna(row[type_col]) else ""

        is_refund = gross < 0 or "refund" in transaction_label

        if is_refund:
            refund_amount = abs(gross)
            if refund_amount > 0:
                records.append(
                    ZohoRecord(
                        customer_name=c_name,
                        gross_amount=0.0,
                        merchant_fee=0.0,
                        refund_amount=refund_amount,
                        invoice_number=inv,
                        fallback_personal_name=None,
                        transaction_type="refund",
                    )
                )
            continue

        if gross > 0:
            records.append(
                ZohoRecord(
                    customer_name=c_name,
                    gross_amount=gross,
                    merchant_fee=fee,
                    refund_amount=0.0,
                    invoice_number=inv,
                    fallback_personal_name=None,
                    transaction_type="payment",
                )
            )

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
    if MASTERLIST_PATH.endswith(".csv"):
        master_df = pd.read_csv(MASTERLIST_PATH)
    else:
        master_df = pd.read_excel(MASTERLIST_PATH)

    master_df.columns = [str(col).strip() for col in master_df.columns]
    master_headers_lower = {str(col).lower(): str(col) for col in master_df.columns}

    ml_name_col = next((master_headers_lower[k] for k in ["account name", "name", "customer name"] if k in master_headers_lower), None)
    ml_num_col = next((master_headers_lower[k] for k in ["account #", "account number", "account no", "account"] if k in master_headers_lower), None)
    ml_term_col = next((master_headers_lower[k] for k in ["payment term", "payment terms", "terms"] if k in master_headers_lower), None)
    ml_ticket_col = next((master_headers_lower[k] for k in ["cs/ps ticket", "ticket", "cs/ps"] if k in master_headers_lower), None)

    if not ml_name_col or not ml_num_col:
        st.error("❌ Could not identify definitive baseline 'Account Name' or 'Account #' tracking headers inside Masterlist spreadsheet.")
        st.stop()

    master_lookup: Dict[str, AccountMasterItem] = {}
    for _, row in master_df.iterrows():
        name_val = str(row[ml_name_col]).strip()
        num_val = str(row[ml_num_col]).strip()

        if not name_val or name_val.lower() == "nan" or not num_val or num_val.lower() == "nan":
            continue

        term_val = str(row.get(ml_term_col, "due-on-receipt")).strip().lower() if ml_term_col else "due-on-receipt"
        ticket_val = str(row.get(ml_ticket_col, "")).strip() if ml_ticket_col else ""

        master_lookup[name_val] = AccountMasterItem(
            account_number=num_val,
            account_name=name_val,
            payment_term=term_val,
            norm_name=normalize_name(name_val),
            norm_ticket=normalize_name(ticket_val),
        )

    # -----------------------------------------------------------------
    # STEP B: EXTRACT INVOICES
    # -----------------------------------------------------------------
    invoice_cache = {}
    invoice_sources_list: List[ZohoRecord] = []

    if uploaded_invoices:
        for inv in uploaded_invoices:
            meta = extract_invoice_metadata_intelligent(inv)
            invoice_number = normalize_invoice_number(meta.get("invoice_number"))

            if invoice_number:
                invoice_cache[invoice_number] = {
                    "resolved_name": meta.get("customer_name"),
                    "fallback_personal_name": meta.get("fallback_personal_name"),
                }

                invoice_sources_list.append(
                    ZohoRecord(
                        customer_name=meta.get("customer_name"),
                        gross_amount=clean_numeric_value(meta.get("gross_amount", 0.0)),
                        merchant_fee=0.0,
                        refund_amount=0.0,
                        invoice_number=invoice_number,
                        fallback_personal_name=meta.get("fallback_personal_name"),
                        transaction_type="payment",
                    )
                )

    # -----------------------------------------------------------------
    # STEP C: PARSE BOA REPORT
    # -----------------------------------------------------------------
    if boa_file.name.lower().endswith(".csv"):
        raw_bytes = boa_file.read()
        lines = raw_bytes.decode("utf-8").splitlines()
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
    desc_target = next((c for c in ["description", "transaction description", "payee", "memo"] if c in boa_df.columns), None)
    date_target = next((c for c in ["posting date", "date", "transaction date"] if c in boa_df.columns), None)
    amount_target = next((c for c in ["net amount", "amount", "net_amount"] if c in boa_df.columns), None)
    account_target = next((c for c in ["source account", "account", "account number", "account_number"] if c in boa_df.columns), None)

    boa_records: List[BOARecord] = []
    for _, row in boa_df.iterrows():
        row_description = str(row.get(desc_target, ""))
        row_net_amount = clean_numeric_value(row.get(amount_target, 0.0))

        if "ZOHO PAYMENTS" in row_description.upper() and row_net_amount > 0:
            parsed_date = datetime.today().strftime("%m/%d/%Y")
            if date_target and pd.notna(row[date_target]):
                try:
                    parsed_date = pd.to_datetime(row[date_target]).strftime("%m/%d/%Y")
                except Exception:
                    pass

            source_account_raw = str(row.get(account_target, "")).strip() if account_target else "3371"
            source_account = re.sub(r"\.0$", "", source_account_raw)

            boa_records.append(
                BOARecord(
                    date=parsed_date,
                    description=row_description,
                    net_amount=row_net_amount,
                    source_account=source_account,
                )
            )

    # -----------------------------------------------------------------
    # STEP D: PARSE ZOHO
    # -----------------------------------------------------------------
    raw_zoho_pool: List[ZohoRecord] = []

    if zoho_file.name.lower().endswith(".pdf"):
        raw_zoho_pool = parse_zoho_summary_pdf_bulletproof(zoho_file)
    else:
        raw_zoho_pool = parse_zoho_excel_or_csv(zoho_file)

    # Refunds often have no invoice number; do not dedupe them away.
    zoho_records: List[ZohoRecord] = []
    payment_by_invoice: Dict[str, ZohoRecord] = {}

    for r in raw_zoho_pool:
        if r.transaction_type == "refund":
            zoho_records.append(r)
            continue

        if r.invoice_number:
            payment_by_invoice[r.invoice_number] = r
        else:
            zoho_records.append(r)

    for inv_rec in invoice_sources_list:
        if not inv_rec.invoice_number:
            continue

        if inv_rec.invoice_number in payment_by_invoice:
            existing = payment_by_invoice[inv_rec.invoice_number]

            if not existing.customer_name and inv_rec.customer_name:
                existing.customer_name = inv_rec.customer_name

            if not existing.fallback_personal_name and inv_rec.fallback_personal_name:
                existing.fallback_personal_name = inv_rec.fallback_personal_name

            if inv_rec.gross_amount > 0:
                existing.gross_amount = inv_rec.gross_amount

            # Critical: do not overwrite merchant_fee here.
            # The Zoho payout source is the authority for fees.
        else:
            payment_by_invoice[inv_rec.invoice_number] = inv_rec

    zoho_records.extend(payment_by_invoice.values())

    with st.expander("🧾 Zoho Parsed Records Debug", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "invoice_number": z.invoice_number,
                        "transaction_type": z.transaction_type,
                        "customer_name": z.customer_name,
                        "fallback_personal_name": z.fallback_personal_name,
                        "gross_amount": z.gross_amount,
                        "merchant_fee": z.merchant_fee,
                        "refund_amount": z.refund_amount,
                    }
                    for z in zoho_records
                ]
            )
        )

    # =====================================================================
    # STEP E: TRANSACTION PROCESSING
    # =====================================================================
    all_journal_lines = []
    validation_errors = []
    diagnostic_logs = []
    fee_correction_logs = []

    for boa_rec in boa_records:
        current_boa_description = str(boa_rec.description)

        payment_records = [z for z in zoho_records if z.transaction_type == "payment" and z.gross_amount > 0]
        refund_records = [z for z in zoho_records if z.transaction_type == "refund" and z.refund_amount > 0]

        if not payment_records:
            validation_errors.append("⚠️ No Zoho payment records were available for this BOA deposit.")
            continue

        total_gross = round(sum(z.gross_amount for z in payment_records), 2)
        total_fees = round(sum(abs(z.merchant_fee) for z in payment_records), 2)
        total_refunds = round(sum(abs(z.refund_amount) for z in refund_records), 2)

        # Safety guard for contaminated fee values.
        # Example: if fee was parsed as 100.79 and refund is 43.54,
        # correct merchant fee becomes 57.25.
        if total_refunds > 0 and total_fees >= total_refunds:
            net_using_fee_only = round(total_gross - total_fees, 2)
            if abs(net_using_fee_only - boa_rec.net_amount) <= 0.01:
                original_total_fees = total_fees
                total_fees = round(total_fees - total_refunds, 2)
                fee_correction_logs.append(
                    {
                        "Gross": total_gross,
                        "BOA Net": boa_rec.net_amount,
                        "Original Merchant Fee": original_total_fees,
                        "Refunds": total_refunds,
                        "Corrected Merchant Fee": total_fees,
                    }
                )

        # If no merchant fee was parsed, infer it only after excluding refunds.
        if total_fees == 0:
            inferred_fee = round(total_gross - total_refunds - boa_rec.net_amount, 2)
            if inferred_fee > 0:
                total_fees = inferred_fee
                fee_correction_logs.append(
                    {
                        "Gross": total_gross,
                        "BOA Net": boa_rec.net_amount,
                        "Refunds": total_refunds,
                        "Inferred Merchant Fee": total_fees,
                    }
                )

        calculated_net = round(total_gross - total_fees - total_refunds, 2)

        if total_gross == 0:
            validation_errors.append("⚠️ **Data Ingestion Alert:** System failed to extract gross amounts.")
            continue

        if total_fees < 0:
            validation_errors.append("🚨 **Mathematical Balance Discrepancy!** Merchant fee became negative.")
            continue

        if abs(calculated_net - boa_rec.net_amount) > 0.01:
            validation_errors.append(
                f"🚨 **Reconciliation mismatch.** Gross {total_gross} - Fees {total_fees} - Refunds {total_refunds} = {calculated_net}, but BOA Net is {boa_rec.net_amount}."
            )
            continue

        offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "B1000002")
        processed_accounts = []

        for z_rec in payment_records:
            norm_biz = normalize_name(z_rec.customer_name)
            norm_per = normalize_name(z_rec.fallback_personal_name)

            matched_master_item = None
            best_score = 0.0
            best_candidate = "No Close Matches"

            # Find the best match instead of stopping at the first acceptable match.
            for item in master_lookup.values():
                s1 = get_match_score(norm_biz, item.norm_name)
                s2 = get_match_score(norm_per, item.norm_name)
                s3 = get_match_score(norm_biz, item.norm_ticket) if item.norm_ticket else 0.0
                s4 = get_match_score(norm_per, item.norm_ticket) if item.norm_ticket else 0.0

                highest_sim_score = max(s1, s2, s3, s4)

                if highest_sim_score > best_score:
                    best_score = highest_sim_score
                    best_candidate = item.account_name
                    matched_master_item = item

            if best_score < 0.85:
                matched_master_item = None

            if not matched_master_item:
                account_num = "21040102-B1000002"
                account_type = "Ledger"
                account_name = "Temporary Receipt"
                cash_code = "AR012"

                display_label = z_rec.customer_name if z_rec.customer_name else (z_rec.fallback_personal_name if z_rec.fallback_personal_name else "Unknown")
                desc = f"{display_label} (UNRECORDED ENTITY)_{current_boa_description}"

                diagnostic_logs.append(
                    {
                        "Invoice": z_rec.invoice_number,
                        "Raw Name Extracted": z_rec.customer_name,
                        "Engine's Target": norm_biz,
                        "Closest Masterlist Match": f"{best_candidate} ({round(best_score * 100, 1)}% Similarity)",
                    }
                )
            else:
                master_item = matched_master_item
                processed_accounts.append(master_item)

                term_info = CASH_CODE_MAPPING.get(master_item.payment_term, CASH_CODE_MAPPING["fallback"])
                cash_code = term_info[0]
                prefix = "MPP " if cash_code == "AR002" else ""

                account_num = master_item.account_number
                account_type = "Customer"
                account_name = master_item.account_name
                desc = f"{prefix}{account_num} {account_name}_{current_boa_description}"

            all_journal_lines.append(
                make_journal_line(
                    boa_rec=boa_rec,
                    account_name=account_name,
                    account_type=account_type,
                    account=account_num,
                    posting_profile="AutoPost" if account_type == "Customer" else "",
                    cash_code=cash_code,
                    description=desc,
                    debit="",
                    credit=z_rec.gross_amount,
                    offset_acct=offset_acct,
                )
            )

        if total_fees > 0:
            if len(processed_accounts) == 1:
                acc = processed_accounts[0]
                fee_desc = f"Zoho Merchant Fee {acc.account_number} {acc.account_name}_{current_boa_description}"
            elif len(processed_accounts) > 1:
                account_strings = ", ".join([f"{a.account_number} {a.account_name}" for a in processed_accounts])
                fee_desc = f"Zoho Merchant Fee {account_strings}_{current_boa_description}"
            else:
                fee_desc = f"Zoho Merchant Fee (Unresolved Suspense Pool Batch)_{current_boa_description}"

            all_journal_lines.append(
                make_journal_line(
                    boa_rec=boa_rec,
                    account_name="Outside Service (Finance)",
                    account_type="Ledger",
                    account="43170111-U26C05001-B735350-UOA003",
                    posting_profile="",
                    cash_code="OSF005",
                    description=fee_desc,
                    debit=total_fees,
                    credit="",
                    offset_acct=offset_acct,
                )
            )

        if total_refunds > 0:
            refund_desc = f"Zoho Refunds_{current_boa_description}"

            all_journal_lines.append(
                make_journal_line(
                    boa_rec=boa_rec,
                    account_name="Refund Clearing",
                    account_type="Ledger",
                    account=REFUND_CLEARING_ACCOUNT,
                    posting_profile="",
                    cash_code="OSF005",
                    description=refund_desc,
                    debit=total_refunds,
                    credit="",
                    offset_acct=offset_acct,
                )
            )

    # STEP F: DISPLAY INTERACTIVE METRICS & EXPORT DOWNLOADS
    if fee_correction_logs:
        with st.expander("🧮 Fee Correction Debug", expanded=False):
            st.dataframe(pd.DataFrame(fee_correction_logs))

    if validation_errors:
        st.error("### Pipeline Validation Discrepancies Checked")
        for error in validation_errors:
            st.markdown(error)

    if all_journal_lines:
        st.success(f"### Transformed {len(all_journal_lines)} Journal Lines Successfully!")
        output_df = pd.DataFrame(all_journal_lines, columns=D365_TEMPLATE_COLUMNS)
        st.dataframe(output_df)

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            output_df.to_excel(writer, index=False, sheet_name="Journal Lines")

        st.download_button(
            label="📥 Download Generated D365 Journal Import Sheet",
            data=buffer.getvalue(),
            file_name="D365_General_Journal_Import.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # =====================================================================
    # DIAGNOSTIC DASHBOARD
    # =====================================================================
    if diagnostic_logs:
        st.markdown("---")
        with st.expander("🚨 🕵️ Unmatched Entities Debugger (Click Here)", expanded=True):
            st.error(
                "**Why are these showing as Temporary Receipt?**\n"
                "The engine could not find a strong enough match. Check the closest Masterlist match."
            )
            st.dataframe(pd.DataFrame(diagnostic_logs))
