import streamlit as st
import pandas as pd
from pypdf import PdfReader
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, date
import re
import io
import os
import difflib
import tempfile
from pathlib import Path

# =============================================================================
# CONFIGURATION / CONSTANTS
# =============================================================================

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

D365_TEMPLATE_COLUMNS = [
    "Date", "Voucher", "Account name", "Company", "Account type", "Account",
    "Posting Profile", "Cash code", "Description", "Debit", "Credit",
    "Item sales tax group", "Sales tax code", "Offset company", "Bank Account Type",
    "Offset account", "Offset transaction text", "Currency", "Exchange rate",
    "Item sales tax group2", "Sales tax group", "Withholding tax group",
    "Release date", "Reversing entry", "Reversing date"
]

REFUND_CLEARING_ACCOUNT = "REFUND-CLEARING-ACCOUNT"  # replace with your real D365 account


# =============================================================================
# MODELS
# =============================================================================

class BOARecord(BaseModel):
    date: date
    description: str
    net_amount: float
    source_account: str


class ZohoRecord(BaseModel):
    customer_name: Optional[str] = None
    description: Optional[str] = None
    gross_amount: float = 0.0
    merchant_fee: float = 0.0
    refund_amount: float = 0.0
    invoice_number: Optional[str] = None
    fallback_personal_name: Optional[str] = None
    transaction_type: str = "payment"   # payment | refund


class AccountMasterItem(BaseModel):
    account_number: str
    account_name: str
    payment_term: str = "due-on-receipt"
    norm_name: str = ""
    norm_ticket: str = ""


# =============================================================================
# HELPERS
# =============================================================================

def money_to_float(val: Any) -> float:
    if pd.isna(val) or val is None:
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    raw = str(val).strip()
    if raw == "":
        return 0.0

    # normalize weird minus signs
    raw = raw.replace("−", "-").replace("–", "-")

    # keep only numeric-ish chars
    raw = re.sub(r"[^0-9,\.\-\(\)]", "", raw)

    is_negative = False
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]

    if raw.startswith("-"):
        is_negative = True
        raw = raw[1:]

    if raw.endswith("-"):
        is_negative = True
        raw = raw[:-1]

    raw = raw.replace(",", "").strip()

    try:
        number = float(raw)
    except ValueError:
        return 0.0

    return -abs(number) if is_negative else number


def normalize_name(name: Any) -> str:
    if name is None or pd.isna(name):
        return ""

    n = str(name).lower()

    # remove common entity suffixes
    n = re.sub(r"\b(inc|llc|corp|ltd|incorporated|company|co|pllc|inc\.|llc\.|corp\.)\b", "", n)

    # remove invoice numbers / money / punctuation
    n = re.sub(r"INV-[A-Za-z0-9\-]+", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\b\d+(?:,\d{3})*\.\d{2}\b", " ", n)
    n = re.sub(r"[^a-z0-9]", "", n)

    return n


def get_match_score(target: str, candidate: str) -> float:
    if not target or not candidate:
        return 0.0
    if target == candidate:
        return 1.0
    if len(target) >= 5 and (target in candidate or candidate in target):
        return 1.0
    return difflib.SequenceMatcher(None, target, candidate).ratio()


def clean_match_text(text: Any) -> str:
    if text is None or pd.isna(text):
        return ""
    t = str(text)
    t = re.sub(r"INV-[A-Za-z0-9\-]+", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d+(?:,\d{3})*\.\d{2}\b", " ", t)
    t = t.replace("—", " ").replace("–", " ").replace("-", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def display_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%m/%d/%Y")
    try:
        return pd.to_datetime(value).strftime("%m/%d/%Y")
    except Exception:
        return str(value)


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
        "Date": display_date(boa_rec.date),
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


def find_header_col(df: pd.DataFrame, options: List[str]) -> Optional[str]:
    headers = {str(c).lower(): str(c) for c in df.columns}
    for opt in options:
        if opt in headers:
            return headers[opt]
    return None


def normalize_zoho_pdf_text(text: str) -> str:
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"(?<=\d),\s+(?=\d{3})", ",", text)      # 3, 102.94 -> 3,102.94
    text = re.sub(r"(?<=\d)\s+\.\s*(?=\d{2})", ".", text)  # 10,657 . 17 -> 10,657.17
    text = re.sub(r"\s+USD\b", " USD", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def choose_best_amount_match(invoice_amount: float, payments: List[ZohoRecord]) -> Optional[ZohoRecord]:
    candidates = [p for p in payments if abs(p.gross_amount - invoice_amount) <= 0.01]
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_master_item(name_or_desc: str, master_lookup: Dict[str, AccountMasterItem]) -> Optional[AccountMasterItem]:
    target_clean = clean_match_text(name_or_desc)
    target = normalize_name(target_clean)
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


def resolve_account_payment_term(account_name: str, form_terms: Dict[str, str], default_term: str) -> str:
    key = normalize_name(account_name)
    if key in form_terms and form_terms[key]:
        return form_terms[key]
    return (default_term or "due-on-receipt").strip().lower()


def get_cash_code_for_term(term: str, cash_term_map: Dict[str, str]) -> str:
    t = (term or "").strip().lower()
    if t in cash_term_map and cash_term_map[t]:
        return cash_term_map[t]
    if t in CASH_CODE_MAPPING:
        return CASH_CODE_MAPPING[t][0]
    return CASH_CODE_MAPPING["fallback"][0]


def load_master_lookup(master_df: pd.DataFrame) -> Dict[str, AccountMasterItem]:
    master_df.columns = [str(c).strip() for c in master_df.columns]
    headers = {str(c).lower(): str(c) for c in master_df.columns}

    name_col = next((headers[k] for k in ["account name", "name", "customer name"] if k in headers), None)
    num_col = next((headers[k] for k in ["account #", "account number", "account no", "account"] if k in headers), None)
    term_col = next((headers[k] for k in ["payment term", "payment terms", "terms"] if k in headers), None)
    ticket_col = next((headers[k] for k in ["cs/ps ticket", "ticket", "cs/ps"] if k in headers), None)

    if not name_col or not num_col:
        raise ValueError("Could not locate Account Name / Account # columns in Account Masterlist.")

    lookup: Dict[str, AccountMasterItem] = {}
    for _, row in master_df.iterrows():
        name_val = str(row.get(name_col, "")).strip()
        num_val = str(row.get(num_col, "")).strip()
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
    Form Master DB:
    - use column I (8-based index 8) for payment term / "Invoice Sent"
    - try to locate a name column for account/customer name
    """
    if not form_master_path or not os.path.exists(form_master_path):
        return {}

    df = pd.read_excel(form_master_path)
    df.columns = [str(c).strip() for c in df.columns]

    if len(df.columns) < 9:
        return {}

    term_col = df.columns[8]  # Column I
    name_col = next(
        (c for c in df.columns if "account" in c.lower() or "customer" in c.lower() or "name" in c.lower()),
        df.columns[0],
    )

    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        acct = str(row.get(name_col, "")).strip()
        term = str(row.get(term_col, "")).strip().lower()
        if acct and acct.lower() != "nan":
            out[normalize_name(acct)] = term
    return out


def load_cash_code_map(cash_master_path: str) -> Dict[str, str]:
    if not cash_master_path or not os.path.exists(cash_master_path):
        return {}

    df = pd.read_excel(cash_master_path)
    df.columns = [str(c).strip() for c in df.columns]

    headers = {str(c).lower(): str(c) for c in df.columns}
    term_col = next((headers[k] for k in ["payment term", "payment terms", "terms"] if k in headers), None)
    code_col = next((headers[k] for k in ["cash code", "cash_code", "code"] if k in headers), None)

    # fallback to first 2 columns if workbook headers are different
    if not term_col and len(df.columns) >= 1:
        term_col = df.columns[0]
    if not code_col and len(df.columns) >= 2:
        code_col = df.columns[1]

    if not term_col or not code_col:
        return {}

    mapping: Dict[str, str] = {}
    for _, row in df.iterrows():
        term = str(row.get(term_col, "")).strip().lower()
        code = str(row.get(code_col, "")).strip()
        if term and code:
            mapping[term] = code
    return mapping


def extract_invoice_metadata_intelligent(pdf_file) -> Dict[str, Any]:
    """
    Extract invoice number, customer/business name, and total amount from invoice PDFs.
    """
    result = {
        "customer_name": None,
        "invoice_number": None,
        "gross_amount": 0.0,
        "fallback_personal_name": None,
    }

    try:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""

        clean_text = " ".join(full_text.split())

        # Invoice number
        inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", clean_text, re.IGNORECASE)
        if inv_match:
            result["invoice_number"] = inv_match.group(1).strip().upper()
        else:
            # fallback to filename
            result["invoice_number"] = re.sub(r"\.pdf$", "", str(getattr(pdf_file, "name", "")), flags=re.IGNORECASE)

        # Amount
        pm_match = re.search(r"Payment\s*Made[^\d\$]*\$?([0-9,]+\.\d{2})", clean_text, re.IGNORECASE)
        if pm_match:
            result["gross_amount"] = money_to_float(pm_match.group(1))
        else:
            total_matches = re.findall(r"Total[^\d\$]*\$?([0-9,]+\.\d{2})", clean_text, re.IGNORECASE)
            if total_matches:
                result["gross_amount"] = money_to_float(total_matches[-1])

        # Name via Bill To / Customer Name / To
        bill_to_match = re.search(
            r"(?:Bill\s*To|Customer\s*Name|To)\s*([A-Za-z0-9\s\.\,\&\-]+?)\s*(?:Ship\s*To|Invoice\s*Date|Terms|Sales\s*person|Balance\s*Due|$)",
            clean_text,
            re.IGNORECASE,
        )
        if bill_to_match:
            candidate = bill_to_match.group(1).strip()
            if len(candidate) > 2:
                result["customer_name"] = candidate
                result["fallback_personal_name"] = candidate

        # fallback: first non-empty line after "Bill To"
        if not result["customer_name"]:
            lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
            idx = None
            for i, ln in enumerate(lines):
                if "bill to" in ln.lower():
                    idx = i
                    break
            if idx is not None:
                for j in range(idx + 1, min(idx + 6, len(lines))):
                    cand = lines[j].strip()
                    if any(x.lower() in cand.lower() for x in ["ship to", "invoice", "terms", "sales person", "balance due"]):
                        break
                    # ignore pure state/zip lines
                    if re.fullmatch(r"[A-Z]{2}\.?\s*\d{5}(-\d{4})?", cand):
                        continue
                    if len(cand) > 2:
                        result["customer_name"] = cand
                        result["fallback_personal_name"] = cand
                        break

    except Exception as e:
        st.error(f"Error executing invoice metadata extraction: {e}")

    finally:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

    return result


def parse_zoho_summary_pdf_bulletproof(pdf_file) -> List[ZohoRecord]:
    """
    Parse Zoho payout PDF rows directly from the 'All Transactions' section.
    Handles payment rows with or without invoice numbers.
    """
    records: List[ZohoRecord] = []
    seen = set()

    try:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""

        text = normalize_zoho_pdf_text(full_text)
        # Transaction headings in the detailed row section
        heading_pattern = re.compile(
            r"\b(Payment|Refund)\b\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+"
            r"\d{1,2}:\d{2}\s+[AP]M",
            re.IGNORECASE,
        )
        money_pattern = re.compile(r"[-+]?\$?[0-9,]+\.\d{2}")

        matches = list(heading_pattern.finditer(text))

        # Fallback if the PDF text is badly broken: try line-by-line parsing
        if not matches:
            for line in text.split(" | "):
                if not re.search(r"\b(Payment|Refund)\b", line, re.IGNORECASE):
                    continue
                chunk = line
                tt = "refund" if "refund" in chunk.lower() else "payment"
                amounts = [money_to_float(m.group(0)) for m in money_pattern.finditer(chunk)]
                if not amounts:
                    continue

                desc = re.split(r"\$?[0-9,]+\.\d{2}", chunk, maxsplit=1)[0].strip()
                desc = re.sub(r"^\b(Payment|Refund)\b\s+.*?\s+[AP]M\s+", "", desc, flags=re.IGNORECASE).strip(" -—")
                inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", desc, re.IGNORECASE)
                inv_id = inv_match.group(1).upper() if inv_match else None

                gross = abs(amounts[0]) if amounts else 0.0
                fee = abs(amounts[1]) if len(amounts) > 1 else 0.0

                key = (tt, round(gross, 2), round(fee, 2), normalize_name(desc))
                if gross > 0 and key not in seen:
                    seen.add(key)
                    records.append(
                        ZohoRecord(
                            customer_name=desc if desc else None,
                            description=desc if desc else None,
                            gross_amount=gross if tt == "payment" else 0.0,
                            merchant_fee=fee if tt == "payment" else 0.0,
                            refund_amount=gross if tt == "refund" else 0.0,
                            invoice_number=inv_id,
                            transaction_type=tt,
                        )
                    )
            return records

        for idx, m in enumerate(matches):
            start = m.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            chunk = text[start:end].strip()

            tt = m.group(1).lower()
            after_heading = chunk[m.end() - m.start():].strip()

            # first money token start for description cut
            first_money = money_pattern.search(after_heading)
            desc_part = after_heading[:first_money.start()].strip() if first_money else after_heading

            # clean description
            desc_part = desc_part.strip(" -—|")
            desc_part = re.sub(r"\s+", " ", desc_part)

            # attempt to pull invoice number from desc
            inv_match = re.search(r"(INV-[A-Za-z0-9\-]+)", desc_part, re.IGNORECASE)
            inv_id = inv_match.group(1).upper() if inv_match else None

            amounts = [money_to_float(m2.group(0)) for m2 in money_pattern.finditer(chunk)]
            if not amounts:
                continue

            # payment row usually has: gross, fee, total
            gross = abs(amounts[0]) if len(amounts) >= 1 else 0.0
            fee = abs(amounts[1]) if len(amounts) >= 2 else 0.0

            if tt == "refund":
                refund_amt = gross
                gross_amt = 0.0
                fee_amt = 0.0
            else:
                refund_amt = 0.0
                gross_amt = gross
                fee_amt = fee

            key = (tt, round(gross_amt, 2), round(fee_amt, 2), normalize_name(desc_part))
            if key in seen:
                continue
            seen.add(key)

            records.append(
                ZohoRecord(
                    customer_name=desc_part if desc_part else None,
                    description=desc_part if desc_part else None,
                    gross_amount=gross_amt,
                    merchant_fee=fee_amt,
                    refund_amount=refund_amt,
                    invoice_number=inv_id,
                    fallback_personal_name=None,
                    transaction_type=tt,
                )
            )

    except Exception as e:
        st.error(f"Error executing Zoho PDF parser: {e}")

    finally:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

    return records


def parse_zoho_summary_table(file_obj) -> List[ZohoRecord]:
    """
    Generic parser for Zoho XLSX/CSV summaries.
    """
    if str(getattr(file_obj, "name", "")).lower().endswith(".csv"):
        df = pd.read_csv(file_obj)
    else:
        df = pd.read_excel(file_obj)

    df.columns = [str(c).strip() for c in df.columns]

    cust_col = find_header_col(df, ["customer", "customer name", "name"])
    gross_col = find_header_col(df, ["gross amount", "gross", "amount"])
    fee_col = find_header_col(df, ["fee", "merchant fee"])
    inv_col = find_header_col(df, ["invoice", "invoice number", "invoice no"])
    desc_col = find_header_col(df, ["description", "transaction description", "notes"])
    type_col = find_header_col(df, ["type", "transaction type"])

    records: List[ZohoRecord] = []
    for _, row in df.iterrows():
        desc = str(row.get(desc_col, "")).strip() if desc_col and pd.notna(row.get(desc_col)) else None
        c_name = str(row.get(cust_col, "")).strip() if cust_col and pd.notna(row.get(cust_col)) else None
        inv_no = str(row.get(inv_col, "")).strip() if inv_col and pd.notna(row.get(inv_col)) else None
        gross = money_to_float(row.get(gross_col, 0.0)) if gross_col else 0.0
        fee = abs(money_to_float(row.get(fee_col, 0.0))) if fee_col else 0.0
        ttype = str(row.get(type_col, "payment")).strip().lower() if type_col and pd.notna(row.get(type_col)) else "payment"

        if gross < 0 or "refund" in ttype:
            records.append(
                ZohoRecord(
                    customer_name=c_name or desc,
                    description=desc or c_name,
                    gross_amount=0.0,
                    merchant_fee=0.0,
                    refund_amount=abs(gross),
                    invoice_number=inv_no,
                    transaction_type="refund",
                )
            )
        else:
            records.append(
                ZohoRecord(
                    customer_name=c_name or desc,
                    description=desc or c_name,
                    gross_amount=gross,
                    merchant_fee=fee,
                    refund_amount=0.0,
                    invoice_number=inv_no,
                    transaction_type="payment",
                )
            )

    return records


# =============================================================================
# APP UI
# =============================================================================

st.set_page_config(page_title="D365 General Journal Automation", layout="wide")
st.title("D365 General Journal Automation Engine")
st.subheader("Daily Operational Reconciliations Matrix")

BASE_DIR = Path(__file__).resolve().parent

masterlist_path = None
for p in [BASE_DIR / "Account Masterlist.xlsx", BASE_DIR / "Account Masterlist.csv"]:
    if p.exists():
        masterlist_path = str(p)
        break

form_master_path = str(BASE_DIR / "Form Master DB.xlsx") if (BASE_DIR / "Form Master DB.xlsx").exists() else None
cash_master_path = str(BASE_DIR / "Cash Code Masterlist.xlsx") if (BASE_DIR / "Cash Code Masterlist.xlsx").exists() else None

if not masterlist_path:
    st.error("Missing Account Masterlist.xlsx or Account Masterlist.csv in the repo root.")
    st.stop()

st.sidebar.header("📅 Daily Variable Inputs")
boa_file = st.sidebar.file_uploader("1. Bank of America Report (Excel/CSV)", type=["xlsx", "csv"])
zoho_file = st.sidebar.file_uploader("2. Zoho Transaction Summary or Direct Invoices (PDF/Excel/CSV)", type=["pdf", "xlsx", "csv"])
uploaded_invoices = st.sidebar.file_uploader("3. Extra Customer Invoices (PDFs) [Optional]", type=["pdf"], accept_multiple_files=True)

if not (boa_file and zoho_file):
    st.info("Upload BOA and Zoho files to continue.")
    st.stop()

# =============================================================================
# LOAD MASTER DATA
# =============================================================================

if masterlist_path.endswith(".csv"):
    master_df = pd.read_csv(masterlist_path)
else:
    master_df = pd.read_excel(masterlist_path)

master_lookup = load_master_lookup(master_df)
form_terms = load_form_master_terms(form_master_path) if form_master_path else {}
cash_term_map = load_cash_code_map(cash_master_path) if cash_master_path else {}

# =============================================================================
# EXTRA INVOICE PDF ENRICHMENT
# =============================================================================

invoice_cache: Dict[str, Dict[str, Any]] = {}
invoice_sources_list: List[ZohoRecord] = []

if uploaded_invoices:
    for inv in uploaded_invoices:
        meta = extract_invoice_metadata_intelligent(inv)
        inv_no = str(meta.get("invoice_number") or "").strip().upper()
        if inv_no:
            invoice_cache[inv_no] = meta
            invoice_sources_list.append(
                ZohoRecord(
                    customer_name=meta.get("customer_name"),
                    description=meta.get("customer_name"),
                    gross_amount=money_to_float(meta.get("gross_amount", 0.0)),
                    merchant_fee=0.0,
                    refund_amount=0.0,
                    invoice_number=inv_no,
                    fallback_personal_name=meta.get("fallback_personal_name"),
                    transaction_type="payment",
                )
            )

# =============================================================================
# PARSE BOA REPORT
# =============================================================================

boa_records: List[BOARecord] = []

try:
    if boa_file.name.lower().endswith(".csv"):
        raw_bytes = boa_file.read()
        lines = raw_bytes.decode("utf-8", errors="ignore").splitlines()
        boa_file.seek(0)

        skip_count = 0
        for idx, line in enumerate(lines):
            if "date" in line.lower() and "description" in line.lower():
                skip_count = idx
                break
        boa_df = pd.read_csv(boa_file, skiprows=skip_count)
    else:
        boa_df = pd.read_excel(boa_file)

    boa_df.columns = [str(c).strip().lower() for c in boa_df.columns]
    desc_col = find_header_col(boa_df, ["description", "transaction description", "payee", "memo"])
    amt_col = find_header_col(boa_df, ["net amount", "amount", "net_amount"])
    date_col = find_header_col(boa_df, ["posting date", "date", "transaction date"])
    acct_col = find_header_col(boa_df, ["source account", "account", "account number", "account_number"])

    if not desc_col or not amt_col:
        st.error("Could not identify BOA description/amount columns.")
        st.stop()

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

            source_account = str(row.get(acct_col, "3371")).strip() if acct_col else "3371"
            source_account = re.sub(r"\.0$", "", source_account)

            boa_records.append(
                BOARecord(
                    date=dt,
                    description=desc,
                    net_amount=amt,
                    source_account=source_account,
                )
            )

except Exception as e:
    st.error(f"Error handling BOA data intake: {e}")
    st.stop()

if not boa_records:
    st.warning("No positive ZOHO PAYMENTS deposits were found in the BOA file.")

# =============================================================================
# PARSE ZOHO
# =============================================================================

raw_zoho_pool: List[ZohoRecord] = []

try:
    if zoho_file.name.lower().endswith(".pdf"):
        raw_zoho_pool = parse_zoho_summary_pdf_bulletproof(zoho_file)
    else:
        # save to temp so pandas can read reliably
        suffix = os.path.splitext(zoho_file.name)[1].lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(zoho_file.getbuffer())
            temp_zoho_path = tmp.name

        try:
            raw_zoho_pool = parse_zoho_summary_table(temp_zoho_path)
        finally:
            try:
                os.remove(temp_zoho_path)
            except Exception:
                pass

except Exception as e:
    st.error(f"Error parsing Zoho source data: {e}")
    st.stop()

# split payments / refunds
payment_records: List[ZohoRecord] = []
refund_records: List[ZohoRecord] = []

for r in raw_zoho_pool:
    if (r.transaction_type or "payment").lower() == "refund":
        refund_records.append(r)
    else:
        payment_records.append(r)

# =============================================================================
# ENRICH PAYMENT ROWS WITH EXTRA INVOICE PDFs
# =============================================================================

# By invoice number first, then by exact gross amount
for inv_rec in invoice_sources_list:
    matched = None

    if inv_rec.invoice_number:
        for p in payment_records:
            if p.invoice_number and p.invoice_number.upper() == inv_rec.invoice_number.upper():
                matched = p
                break

    if not matched:
        matched = choose_best_amount_match(inv_rec.gross_amount, payment_records)

    if matched:
        if not matched.customer_name and inv_rec.customer_name:
            matched.customer_name = inv_rec.customer_name
        if not matched.description and inv_rec.description:
            matched.description = inv_rec.description
        if not matched.fallback_personal_name and inv_rec.fallback_personal_name:
            matched.fallback_personal_name = inv_rec.fallback_personal_name
        if not matched.invoice_number:
            matched.invoice_number = inv_rec.invoice_number

# =============================================================================
# RESOLVE MISSING CUSTOMER/BUSINESS NAMES FROM DESCRIPTION
# =============================================================================

for p in payment_records:
    if not p.customer_name:
        candidate_text = p.description or p.fallback_personal_name or ""
        if candidate_text:
            resolved = resolve_master_item(candidate_text, master_lookup)
            if resolved:
                p.customer_name = resolved.account_name

# =============================================================================
# RECONCILE + BUILD JOURNAL LINES
# =============================================================================

all_journal_lines: List[Dict[str, Any]] = []
validation_errors: List[str] = []
diagnostic_logs: List[Dict[str, Any]] = []

gross_total = round(sum(x.gross_amount for x in payment_records), 2)
fee_total = round(sum(abs(x.merchant_fee) for x in payment_records), 2)
refund_total = round(sum(abs(x.refund_amount) for x in refund_records), 2)
calculated_net = round(gross_total - fee_total - refund_total, 2)

if boa_records:
    boa_net = round(sum(b.net_amount for b in boa_records), 2)
    if abs(calculated_net - boa_net) > 0.01:
        validation_errors.append(
            f"🚨 Reconciliation mismatch. Gross {gross_total} - Fees {fee_total} - Refunds {refund_total} = {calculated_net}, but BOA Net is {boa_net}."
        )

for boa_rec in boa_records:
    offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "B1000002")
    processed_accounts: List[AccountMasterItem] = []

    # payments
    for p in payment_records:
        current_desc = boa_rec.description
        candidate_text = p.customer_name or p.fallback_personal_name or p.description or ""
        resolved_item = resolve_master_item(candidate_text, master_lookup)

        if resolved_item:
            processed_accounts.append(resolved_item)
            account_num = resolved_item.account_number
            account_name = resolved_item.account_name
            payment_term = resolve_account_payment_term(account_name, form_terms, resolved_item.payment_term)
            cash_code = get_cash_code_for_term(payment_term, cash_term_map)
            account_type = "Customer"
            posting_profile = "AutoPost"
            prefix = "MPP " if cash_code == "AR002" else ""
            desc = f"{prefix}{account_num} {account_name}_{current_desc}"
        else:
            account_num = "21040102-B1000002"
            account_name = "Temporary Receipt"
            cash_code = "AR012"
            account_type = "Ledger"
            posting_profile = ""
            desc = f"{candidate_text} (UNRECORDED ENTITY)_{current_desc}"

            diagnostic_logs.append({
                "Invoice": p.invoice_number,
                "Raw Name Extracted": p.customer_name,
                "Description": p.description,
                "Closest Masterlist Match": "No exact match"
            })

        all_journal_lines.append(
            make_journal_line(
                boa_rec=boa_rec,
                account_name=account_name,
                account_type=account_type,
                account=account_num,
                posting_profile=posting_profile,
                cash_code=cash_code,
                description=desc,
                debit="",
                credit=p.gross_amount,
                offset_acct=offset_acct,
            )
        )

    # merchant fee
    if fee_total > 0:
        if len(processed_accounts) == 1:
            acc = processed_accounts[0]
            fee_desc = f"Zoho Merchant Fee {acc.account_number} {acc.account_name}_{boa_rec.description}"
        elif len(processed_accounts) > 1:
            account_strings = ", ".join([f"{a.account_number} {a.account_name}" for a in processed_accounts])
            fee_desc = f"Zoho Merchant Fee {account_strings}_{boa_rec.description}"
        else:
            fee_desc = f"Zoho Merchant Fee (Unresolved Suspense Pool Batch)_{boa_rec.description}"

        all_journal_lines.append(
            make_journal_line(
                boa_rec=boa_rec,
                account_name="Outside Service (Finance)",
                account_type="Ledger",
                account="43170111-U26C05001-B735350-UOA003",
                posting_profile="",
                cash_code="OSF005",
                description=fee_desc,
                debit=fee_total,
                credit="",
                offset_acct=offset_acct,
            )
        )

    # refunds
    if refund_total > 0:
        all_journal_lines.append(
            make_journal_line(
                boa_rec=boa_rec,
                account_name="Refund Clearing",
                account_type="Ledger",
                account=REFUND_CLEARING_ACCOUNT,
                posting_profile="",
                cash_code="OSF005",
                description=f"Zoho Refunds_{boa_rec.description}",
                debit=refund_total,
                credit="",
                offset_acct=offset_acct,
            )
        )

# =============================================================================
# OUTPUT
# =============================================================================

if validation_errors:
    st.error("### Pipeline Validation Discrepancies Checked")
    for e in validation_errors:
        st.markdown(e)

with st.expander("🧾 Parsed Record Debug", expanded=False):
    st.write("Gross Total:", gross_total)
    st.write("Fee Total:", fee_total)
    st.write("Refund Total:", refund_total)
    st.write("Calculated Net:", calculated_net)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "type": z.transaction_type,
                    "invoice_number": z.invoice_number,
                    "customer_name": z.customer_name,
                    "description": z.description,
                    "gross_amount": z.gross_amount,
                    "merchant_fee": z.merchant_fee,
                    "refund_amount": z.refund_amount,
                }
                for z in (payment_records + refund_records)
            ]
        )
    )

if diagnostic_logs:
    with st.expander("🚨 🕵️ Unmatched Entities Debugger", expanded=False):
        st.dataframe(pd.DataFrame(diagnostic_logs))

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
