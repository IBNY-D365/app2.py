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
            cleaned_gross = str(row.get(gross_col, 0.0)).strip().replace('$', '').replace(',', '')
            cleaned_fee = str(row.get(fee_col, 0.0)).strip().replace('$', '').replace(',', '')
            raw_zoho_pool.append(ZohoRecord(
                customer_name=str(row[cust_col]).strip() if cust_col and pd.notna(row[cust_col]) else None,
                gross_amount=float(cleaned_gross) if cleaned_gross else 0.0,
                merchant_fee=float(cleaned_fee) if cleaned_fee else 0.0,
                invoice_number=str(row[inv_col]).strip() if inv_col and pd.notna(row[inv_col]) else None
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
    # STEP E: TRANSACTION MATCHING ENGINE
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
            validation_errors.append("⚠️ **Data Ingestion Alert:** Gross totals returned zero balance calculations.")
            continue
            
        if total_fees < 0:
            validation_errors.append(f"🚨 **Mathematical Balance Discrepancy!** Bank Net ledger holds higher metrics than source records.")
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
            
            # CORE FIX LOGIC: Running the AI Fuzzy Similarity Matrix checking routines
            for item in master_lookup.values():
                s1 = get_match_score(norm_biz, item.norm_name)
                s2 = get_match_score(norm_per, item.norm_name)
                s3 = get_match_score(norm_biz, item.norm_ticket) if item.norm_ticket else 0.0
                s4 = get_match_score(norm_per, item.norm_ticket) if item.norm_ticket else 0.0
                
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
                
                display_label = z_rec.customer_name if z_rec.customer_name else (z_rec.fallback_personal_name if z_rec.fallback_personal_name else "Unknown")
                desc = f"{display_label} (UNRECORDED ENTITY)_{current_boa_description}"
                
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

    # STEP F: DATA RENDERING AND DISTRIBUTION PLATFORM
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
        
    if diagnostic_logs:
        st.markdown("---")
        with st.expander("🚨 🕵️ Unmatched Entities Debugger (Click Here)", expanded=True):
            st.error("The automated parsing core could not locate high percentage similarities inside database mappings.")
            st.dataframe(pd.DataFrame(diagnostic_logs))
