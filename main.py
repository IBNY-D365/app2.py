import streamlit as st
import pandas as pd
from datetime import datetime
import io
import os
import re
import tempfile
from typing import List, Dict, Any
from pypdf import PdfReader

# Import configurations and mappings
from mappings import CASH_CODE_MAPPING, OFFSET_ACCOUNT_ROUTING, D365_TEMPLATE_COLUMNS

# Import structural models
from core.models import BOARecord, ZohoRecord, AccountMasterItem

# Import custom parsers
from parsers.invoice_parser import (
    extract_invoice_metadata_intelligent,
    parse_zoho_summary_pdf_bulletproof,
)
from parsers.zoho_parser import ZohoParser

# Import verification utilities
from core.validators import normalize_name, get_match_score


# =====================================================================
# GENERAL HELPERS
# =====================================================================

MONEY_PATTERN = re.compile(
    r"\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?"
)


def money_to_float(value: Any) -> float:
    """
    Converts accounting-style money strings into floats.

    Handles:
    $1,963.75
    -$43.54
    ($43.54)
    43.54-
    """
    if pd.isna(value) or value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip()

    if raw == "":
        return 0.0

    is_negative = False

    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]

    raw = raw.replace("−", "-")

    if raw.startswith("-"):
        is_negative = True

    if raw.endswith("-"):
        is_negative = True

    raw = (
        raw.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("+", "")
        .replace("-", "")
        .strip()
    )

    try:
        number = float(raw)
    except ValueError:
        return 0.0

    return -abs(number) if is_negative else number


def normalize_invoice_number(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip().upper()

    if text == "" or text.lower() == "nan":
        return None

    text = re.sub(r"\.0$", "", text)
    return text


def display_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%m/%d/%Y")

    try:
        return pd.to_datetime(value).strftime("%m/%d/%Y")
    except Exception:
        return str(value)


def coerce_zoho_record(record: Any) -> ZohoRecord:
    """
    Ensures all records coming from old parsers, PDF parsers, or updated
    parsers become the current core.models.ZohoRecord shape.
    """
    customer_name = getattr(record, "customer_name", None)
    invoice_number = normalize_invoice_number(getattr(record, "invoice_number", None))
    fallback_personal_name = getattr(record, "fallback_personal_name", None)

    gross_amount = money_to_float(getattr(record, "gross_amount", 0.0))
    merchant_fee = abs(money_to_float(getattr(record, "merchant_fee", 0.0)))
    refund_amount = abs(money_to_float(getattr(record, "refund_amount", 0.0)))

    transaction_type = getattr(record, "transaction_type", "payment") or "payment"
    transaction_type = str(transaction_type).strip().lower()

    # Defensive handling:
    # If any parser returns a negative gross amount, treat it as a refund.
    if gross_amount < 0 or transaction_type == "refund":
        return ZohoRecord(
            customer_name=customer_name,
            gross_amount=0.0,
            merchant_fee=0.0,
            refund_amount=refund_amount if refund_amount > 0 else abs(gross_amount),
            invoice_number=invoice_number,
            fallback_personal_name=fallback_personal_name,
            transaction_type="refund",
        )

    return ZohoRecord(
        customer_name=customer_name,
        gross_amount=gross_amount,
        merchant_fee=merchant_fee,
        refund_amount=0.0,
        invoice_number=invoice_number,
        fallback_personal_name=fallback_personal_name,
        transaction_type="payment",
    )


def parse_refunds_from_zoho_pdf(pdf_file) -> List[ZohoRecord]:
    """
    Supplemental parser for Zoho payout PDFs.

    The existing PDF parser usually captures payment gross and fee,
    but it often misses payout-level refund rows. This function captures
    the Refunds summary row and returns a separate refund record.

    Example Zoho summary:
    Refunds 1 -$43.54 $0.00 -$43.54
    """
    refund_records: List[ZohoRecord] = []

    try:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

        reader = PdfReader(pdf_file)
        full_text = ""

        for page in reader.pages:
            full_text += page.extract_text() or ""

        flat_text = re.sub(r"\s+", " ", full_text)

        # First attempt: summary row like:
        # Refunds 1 -$43.54 $0.00 -$43.54
        refund_summary_match = re.search(
            r"\bRefunds?\b\s+\d+\s+"
            r"((?:\(?\s*[-+]?\s*\$?\s*[0-9,]+\.\d{2}\s*\)?\s*){1,4})",
            flat_text,
            re.IGNORECASE,
        )

        refund_text = ""

        if refund_summary_match:
            refund_text = refund_summary_match.group(0)
        else:
            # Fallback: take text after Refunds and before common next sections.
            fallback_match = re.search(
                r"\bRefunds?\b(.{0,500}?)(?:\bTotal\b|\bTransactions\b|\bPayments\b|$)",
                flat_text,
                re.IGNORECASE,
            )

            if fallback_match:
                refund_text = fallback_match.group(0)

        if refund_text:
            amount_tokens = MONEY_PATTERN.findall(refund_text)
            values = [money_to_float(token) for token in amount_tokens]

            negative_values = [abs(v) for v in values if v < 0]
            nonzero_values = [abs(v) for v in values if abs(v) > 0]

            refund_total = 0.0

            if negative_values:
                # In Zoho payout summaries, the first negative value after
                # Refunds is the refund gross amount.
                refund_total = negative_values[0]
            elif nonzero_values:
                refund_total = nonzero_values[0]

            if refund_total > 0:
                refund_records.append(
                    ZohoRecord(
                        customer_name="Zoho Refund",
                        gross_amount=0.0,
                        merchant_fee=0.0,
                        refund_amount=refund_total,
                        invoice_number=None,
                        fallback_personal_name=None,
                        transaction_type="refund",
                    )
                )

    except Exception as e:
        st.warning(f"Refund parser could not read Zoho PDF refund details: {e}")

    finally:
        try:
            pdf_file.seek(0)
        except Exception:
            pass

    return refund_records


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


# =====================================================================
# STREAMLIT INTERFACE SETUP
# =====================================================================

st.set_page_config(page_title="D365 General Journal Automation", layout="wide")
st.title("D365 General Journal Automation Engine")
st.subheader("Daily Operational Reconciliations Matrix")

possible_paths = ["Account Masterlist.xlsx", "Account Masterlist.csv"]
MASTERLIST_PATH = next((p for p in possible_paths if os.path.exists(p)), None)

if not MASTERLIST_PATH:
    st.error(
        "❌ Core configuration file `Account Masterlist.xlsx` or `.csv` "
        "missing from your repository root folder."
    )
    st.stop()

st.sidebar.header("📅 Daily Variable Inputs")
boa_file = st.sidebar.file_uploader(
    "1. Bank of America Report (Excel/CSV)",
    type=["xlsx", "csv"],
)
zoho_file = st.sidebar.file_uploader(
    "2. Zoho Transaction Summary or Direct Invoices (PDF/Excel/CSV)",
    type=["pdf", "xlsx", "csv"],
)
uploaded_invoices = st.sidebar.file_uploader(
    "3. Extra Customer Invoices (PDFs) [Optional]",
    type=["pdf"],
    accept_multiple_files=True,
)

if not (boa_file and zoho_file):
    st.info(
        "💡 Staging required: Please drop today's Bank of America report "
        "and matching Zoho summary sheet into the sidebar container panel."
    )

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

    ml_name_col = next(
        (
            master_headers_lower[k]
            for k in ["account name", "name", "customer name"]
            if k in master_headers_lower
        ),
        None,
    )

    ml_num_col = next(
        (
            master_headers_lower[k]
            for k in ["account #", "account number", "account no", "account"]
            if k in master_headers_lower
        ),
        None,
    )

    ml_term_col = next(
        (
            master_headers_lower[k]
            for k in ["payment term", "payment terms", "terms"]
            if k in master_headers_lower
        ),
        None,
    )

    ml_ticket_col = next(
        (
            master_headers_lower[k]
            for k in ["cs/ps ticket", "ticket", "cs/ps"]
            if k in master_headers_lower
        ),
        None,
    )

    if not ml_name_col or not ml_num_col:
        st.error(
            "❌ Could not identify definitive baseline 'Account Name' or "
            "'Account #' tracking headers inside Masterlist spreadsheet."
        )
        st.stop()

    master_lookup: Dict[str, AccountMasterItem] = {}

    for _, row in master_df.iterrows():
        name_val = str(row[ml_name_col]).strip()
        num_val = str(row[ml_num_col]).strip()

        if not name_val or name_val.lower() == "nan":
            continue

        if not num_val or num_val.lower() == "nan":
            continue

        term_val = (
            str(row.get(ml_term_col, "due-on-receipt")).strip().lower()
            if ml_term_col
            else "due-on-receipt"
        )

        ticket_val = (
            str(row.get(ml_ticket_col, "")).strip()
            if ml_ticket_col
            else ""
        )

        master_lookup[name_val] = AccountMasterItem(
            account_number=num_val,
            account_name=name_val,
            payment_term=term_val,
            norm_name=normalize_name(name_val),
            norm_ticket=normalize_name(ticket_val),
        )

    # -----------------------------------------------------------------
    # STEP B: EXTRACT EXTRA CUSTOMER INVOICES
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
                        gross_amount=money_to_float(meta.get("gross_amount", 0.0)),
                        merchant_fee=0.0,
                        refund_amount=0.0,
                        invoice_number=invoice_number,
                        fallback_personal_name=meta.get("fallback_personal_name"),
                        transaction_type="payment",
                    )
                )

    # -----------------------------------------------------------------
    # STEP C: PARSE BANK OF AMERICA REPORT
    # -----------------------------------------------------------------

    boa_records: List[BOARecord] = []

    try:
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

        desc_target = next(
            (
                c
                for c in ["description", "transaction description", "payee", "memo"]
                if c in boa_df.columns
            ),
            None,
        )

        date_target = next(
            (
                c
                for c in ["posting date", "date", "transaction date"]
                if c in boa_df.columns
            ),
            None,
        )

        amount_target = next(
            (
                c
                for c in ["net amount", "amount", "net_amount"]
                if c in boa_df.columns
            ),
            None,
        )

        account_target = next(
            (
                c
                for c in ["source account", "account", "account number", "account_number"]
                if c in boa_df.columns
            ),
            None,
        )

        if not desc_target or not amount_target:
            st.error(
                "❌ Could not find required BOA columns. Expected a description "
                "column and an amount/net amount column."
            )
            st.stop()

        for _, row in boa_df.iterrows():
            row_description = str(row.get(desc_target, ""))
            row_net_amount = money_to_float(row.get(amount_target, 0.0))

            if "ZOHO PAYMENTS" in row_description.upper() and row_net_amount > 0:
                parsed_date = datetime.today().date()

                if date_target and pd.notna(row[date_target]):
                    try:
                        parsed_date = pd.to_datetime(row[date_target]).date()
                    except Exception:
                        pass

                raw_source_account = (
                    str(row.get(account_target, "")).strip()
                    if account_target
                    else "3371"
                )

                source_account = re.sub(r"\.0$", "", raw_source_account)

                boa_records.append(
                    BOARecord(
                        date=parsed_date,
                        description=row_description,
                        net_amount=row_net_amount,
                        source_account=source_account,
                    )
                )

    except Exception as e:
        st.error(f"Error handling BOA data intake stream: {e}")
        st.stop()

    if not boa_records:
        st.warning("No positive ZOHO PAYMENTS deposits were found in the BOA file.")

    # -----------------------------------------------------------------
    # STEP D: PARSE ZOHO PAYMENTS SOURCE DATA
    # -----------------------------------------------------------------

    raw_zoho_pool: List[ZohoRecord] = []

    try:
        zoho_filename = zoho_file.name.lower()

        if zoho_filename.endswith(".pdf"):
            try:
                zoho_file.seek(0)
            except Exception:
                pass

            raw_zoho_pool = [
                coerce_zoho_record(record)
                for record in parse_zoho_summary_pdf_bulletproof(zoho_file)
            ]

            # Add refund record from the payout summary if the main PDF parser missed it.
            has_refund = any(
                z.transaction_type == "refund" and z.refund_amount > 0
                for z in raw_zoho_pool
            )

            if not has_refund:
                refund_records_from_pdf = parse_refunds_from_zoho_pdf(zoho_file)
                raw_zoho_pool.extend(refund_records_from_pdf)

        else:
            suffix = os.path.splitext(zoho_file.name)[1]

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(zoho_file.getbuffer())
                temp_zoho_path = tmp.name

            try:
                raw_zoho_pool = [
                    coerce_zoho_record(record)
                    for record in ZohoParser.parse_summary(temp_zoho_path)
                ]
            finally:
                try:
                    os.remove(temp_zoho_path)
                except Exception:
                    pass

    except Exception as e:
        st.error(f"Error parsing Zoho source data: {e}")
        raw_zoho_pool = []

    # -----------------------------------------------------------------
    # STEP D2: MERGE ZOHO PAYMENTS WITH EXTRA INVOICE PDF DATA
    # -----------------------------------------------------------------
    # Important:
    # Refunds often do not have invoice numbers.
    # Therefore, never dedupe refunds by invoice number.
    # -----------------------------------------------------------------

    zoho_records: List[ZohoRecord] = []
    payment_by_invoice: Dict[str, ZohoRecord] = {}

    for raw_record in raw_zoho_pool:
        rec = coerce_zoho_record(raw_record)

        if rec.transaction_type == "refund":
            zoho_records.append(rec)
            continue

        if rec.invoice_number:
            payment_by_invoice[rec.invoice_number] = rec
        else:
            zoho_records.append(rec)

    for inv_rec in invoice_sources_list:
        inv_rec = coerce_zoho_record(inv_rec)

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

            # Critical:
            # Do NOT overwrite existing.merchant_fee here.
            # The Zoho payout source is the authority for merchant fees.

        else:
            payment_by_invoice[inv_rec.invoice_number] = inv_rec

    zoho_records.extend(payment_by_invoice.values())

    # Debug panel: this proves whether refund and fee are separated.
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
    # STEP E: TRANSACTION MATCHING ENGINE
    # =====================================================================

    all_journal_lines = []
    validation_errors = []
    diagnostic_logs = []
    fee_correction_logs = []

    for boa_rec in boa_records:
        current_boa_description = str(boa_rec.description)

        payment_records = [
            z
            for z in zoho_records
            if z.transaction_type == "payment" and z.gross_amount > 0
        ]

        refund_records = [
            z
            for z in zoho_records
            if z.transaction_type == "refund" and z.refund_amount > 0
        ]

        if not payment_records:
            validation_errors.append(
                "⚠️ No Zoho payment records were available for this BOA deposit."
            )
            continue

        total_gross = round(
            sum(z.gross_amount for z in payment_records),
            2,
        )

        total_fees = round(
            sum(abs(z.merchant_fee) for z in payment_records),
            2,
        )

        total_refunds = round(
            sum(abs(z.refund_amount) for z in refund_records),
            2,
        )

        # -----------------------------------------------------------------
        # Safety guard:
        # If a previous parser contaminated merchant fee by including refunds,
        # this detects the exact pattern:
        #
        # gross - contaminated_fee = bank_net
        #
        # Example:
        # 1963.75 - 100.79 = 1862.96
        #
        # Since refunds are now separately parsed, correct fee:
        # 100.79 - 43.54 = 57.25
        # -----------------------------------------------------------------

        if total_refunds > 0 and total_fees >= total_refunds:
            net_using_fee_only = round(total_gross - total_fees, 2)

            if abs(net_using_fee_only - boa_rec.net_amount) <= 0.01:
                original_total_fees = total_fees
                total_fees = round(total_fees - total_refunds, 2)

                fee_correction_logs.append(
                    {
                        "BOA Net": boa_rec.net_amount,
                        "Gross": total_gross,
                        "Original Merchant Fee": original_total_fees,
                        "Refunds": total_refunds,
                        "Corrected Merchant Fee": total_fees,
                        "Reason": "Merchant fee appeared to include refund amount.",
                    }
                )

        # Conservative fallback:
        # If no fee was parsed but the bank reconciliation proves a fee exists,
        # infer fee only after excluding refunds.
        if total_fees == 0:
            inferred_fee = round(
                total_gross - total_refunds - boa_rec.net_amount,
                2,
            )

            if inferred_fee > 0:
                total_fees = inferred_fee

                fee_correction_logs.append(
                    {
                        "BOA Net": boa_rec.net_amount,
                        "Gross": total_gross,
                        "Refunds": total_refunds,
                        "Inferred Merchant Fee": total_fees,
                        "Reason": "No merchant fee was parsed; inferred after excluding refunds.",
                    }
                )

        calculated_net = round(
            total_gross - total_fees - total_refunds,
            2,
        )

        if total_gross == 0:
            validation_errors.append(
                "⚠️ Data Ingestion Alert: Gross totals returned zero."
            )
            continue

        if total_fees < 0:
            validation_errors.append(
                "🚨 Mathematical Balance Discrepancy: merchant fee became negative."
            )
            continue

        if abs(calculated_net - boa_rec.net_amount) > 0.01:
            validation_errors.append(
                f"🚨 Reconciliation mismatch. "
                f"Gross {total_gross} - Fees {total_fees} - Refunds {total_refunds} "
                f"= Calculated Net {calculated_net}, but BOA Net is {boa_rec.net_amount}."
            )
            continue

        offset_acct = OFFSET_ACCOUNT_ROUTING.get(
            boa_rec.source_account,
            "B1000002",
        )

        processed_accounts = []

        # --------------------------------------------------
        # CUSTOMER PAYMENT JOURNAL LINES
        # --------------------------------------------------

        for z_rec in payment_records:
            norm_biz = normalize_name(z_rec.customer_name)
            norm_per = normalize_name(z_rec.fallback_personal_name)

            matched_master_item = None
            best_score = 0.0
            best_candidate = "No Close Matches"

            for item in master_lookup.values():
                item_norm_name = getattr(
                    item,
                    "norm_name",
                    normalize_name(item.account_name),
                )

                item_norm_ticket = getattr(
                    item,
                    "norm_ticket",
                    "",
                )

                s1 = get_match_score(norm_biz, item_norm_name)
                s2 = get_match_score(norm_per, item_norm_name)
                s3 = get_match_score(norm_biz, item_norm_ticket) if item_norm_ticket else 0.0
                s4 = get_match_score(norm_per, item_norm_ticket) if item_norm_ticket else 0.0

                highest_sim_score = max(s1, s2, s3, s4)

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

                display_label = (
                    z_rec.customer_name
                    if z_rec.customer_name
                    else (
                        z_rec.fallback_personal_name
                        if z_rec.fallback_personal_name
                        else "Unknown"
                    )
                )

                desc = (
                    f"{display_label} "
                    f"(UNRECORDED ENTITY)_{current_boa_description}"
                )

                diagnostic_logs.append(
                    {
                        "Invoice": z_rec.invoice_number,
                        "Raw Name Extracted": z_rec.customer_name,
                        "Engine's Target": norm_biz,
                        "Closest Masterlist Match": (
                            f"{best_candidate} "
                            f"({round(best_score * 100, 1)}% Similarity)"
                        ),
                    }
                )

            else:
                master_item = matched_master_item
                processed_accounts.append(master_item)

                term_info = CASH_CODE_MAPPING.get(
                    master_item.payment_term,
                    CASH_CODE_MAPPING["fallback"],
                )

                cash_code = term_info[0]
                prefix = "MPP " if cash_code == "AR002" else ""

                account_num = master_item.account_number
                account_type = "Customer"
                account_name = master_item.account_name

                desc = (
                    f"{prefix}{account_num} "
                    f"{account_name}_{current_boa_description}"
                )

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

        # --------------------------------------------------
        # MERCHANT FEE JOURNAL LINE
        # --------------------------------------------------

        if total_fees > 0:
            if len(processed_accounts) == 1:
                acc = processed_accounts[0]
                fee_desc = (
                    f"Zoho Merchant Fee "
                    f"{acc.account_number} {acc.account_name}_"
                    f"{current_boa_description}"
                )

            elif len(processed_accounts) > 1:
                account_strings = ", ".join(
                    [
                        f"{a.account_number} {a.account_name}"
                        for a in processed_accounts
                    ]
                )

                fee_desc = (
                    f"Zoho Merchant Fee "
                    f"{account_strings}_{current_boa_description}"
                )

            else:
                fee_desc = (
                    f"Zoho Merchant Fee "
                    f"(Unresolved Suspense Pool Batch)_"
                    f"{current_boa_description}"
                )

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

        # --------------------------------------------------
        # REFUND JOURNAL LINE
        # --------------------------------------------------

        if total_refunds > 0:
            refund_desc = f"Zoho Refunds_{current_boa_description}"

            all_journal_lines.append(
                make_journal_line(
                    boa_rec=boa_rec,
                    account_name="Refund Clearing",
                    account_type="Ledger",

                    # Replace this with the correct D365 refund account.
                    account="REFUND-CLEARING-ACCOUNT",

                    posting_profile="",
                    cash_code="OSF005",
                    description=refund_desc,
                    debit=total_refunds,
                    credit="",
                    offset_acct=offset_acct,
                )
            )

    # -----------------------------------------------------------------
    # STEP F: DATA RENDERING AND EXPORT
    # -----------------------------------------------------------------

    if fee_correction_logs:
        with st.expander("🧮 Fee Correction Debug", expanded=False):
            st.dataframe(pd.DataFrame(fee_correction_logs))

    if validation_errors:
        st.error("### Pipeline Validation Discrepancies Checked")
        for error in validation_errors:
            st.markdown(error)

    if all_journal_lines:
        st.success(
            f"### Transformed {len(all_journal_lines)} Journal Lines Successfully!"
        )

        output_df = pd.DataFrame(
            all_journal_lines,
            columns=D365_TEMPLATE_COLUMNS,
        )

        st.dataframe(output_df)

        buffer = io.BytesIO()

        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            output_df.to_excel(
                writer,
                index=False,
                sheet_name="Journal Lines",
            )

        st.download_button(
            label="📥 Download Generated D365 Journal Import Sheet",
            data=buffer.getvalue(),
            file_name="D365_General_Journal_Import.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if diagnostic_logs:
        st.markdown("---")

        with st.expander("🚨 🕵️ Unmatched Entities Debugger", expanded=True):
            st.error(
                "The automated parsing core could not locate high percentage "
                "similarities inside database mappings."
            )
            st.dataframe(pd.DataFrame(diagnostic_logs))
