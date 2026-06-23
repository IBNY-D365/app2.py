import streamlit as st
import pandas as pd
from datetime import datetime
import io
import os

# Import configurations and mappings
from mappings import CASH_CODE_MAPPING, OFFSET_ACCOUNT_ROUTING, D365_TEMPLATE_COLUMNS
# Import structural models
from core.models import BOARecord, ZohoRecord, AccountMasterItem
# Import custom parsers
from parsers.boa_parser import BOAParser
from parsers.invoice_parser import extract_invoice_metadata_intelligent, parse_zoho_summary_pdf_bulletproof
# Import verification utilities
from core.validators import normalize_name, get_match_score

# =====================================================================
# STREAMLIT INTERFACE SETUP
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
    # STEP B: EXTRACT EXTRA INVOICES INTO CACHE
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
    # STEP C: PARSE BANK OF AMERICA REPORT
    # -----------------------------------------------------------------
    # Using the standardized logic mapped via parser definitions
    boa_records = []
    try:
        # temporary check logic mapped out smoothly for compatibility
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

        for _, row in boa_df.iterrows():
            row_description = str(row.get(desc_target, ''))
            # Safe numeric conversion line logic
            cleaned_str = str(row.get(amount_target, 0.0)).strip().replace('$', '').replace(',', '')
            try:
                row_net_amount = float(cleaned_str)
            except ValueError:
                row_net_amount = 0.0
            
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
    except Exception as e:
        st.error(f"Error handling BOA data intake stream: {e}")

    # -----------------------------------------------------------------
    # STEP D: PARSE ZOHO PAYMENTS SOURCE DATA
    # -----------------------------------------------------------------
    raw_zoho_pool: List[ZohoRecord] = []
    if zoho_file.name.endswith('.pdf'):
        raw_zoho_pool = parse_zoho_summary_pdf_bulletproof(zoho_file)
    else:
        # Excel/CSV fallbacks utilizing clean runtime data
        if zoho_file.name.endswith('.csv'):
            zoho_df = pd.read_csv(zoho_file)
        else:
            zoho_df = pd.read_excel(zoho_file)
        zoho_df.columns = [str(c).strip() for c in zoho_df.columns]
        
        cust_col = next((c for c in zoho_df.columns if 'customer' in c.lower()), None)
        gross_col = next((c for c in zoho_df.columns if 'gross' in c.lower() or ('amount' in c.lower() and 'net' not in c.lower())), None)
        fee_col = next((c for c in zoho_df.columns if 'fee' in c.lower()), None)
        inv_col = next((c for c in zoho_df.columns if 'invoice' in c.lower()), None)
        
        for _, row in zoho_df.iterrows():
            cleaned_gross = str(row.get(gross_col, 0.0)).strip().replace('$', '').replace(',',
