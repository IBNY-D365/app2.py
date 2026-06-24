import streamlit as st
import pandas as pd
import io
import pdfplumber  # Added for PDF extraction
import re

# --- 1. CONFIGURATION & CONSTANTS ---
st.set_page_config(page_title="D365 Journal Entry Automation", layout="wide")

D365_COLUMNS = [
    "Date", "Voucher", "Account name", "Company", "Account type", 
    "Account", "Posting Profile", "Cash code", "Description", "Debit", 
    "Credit", "Item sales tax group", "Sales tax code", "Offset company", 
    "Bank Account Type", "Offset account", "Offset transaction text", 
    "Currency", "Exchange rate", "Item sales tax group2", "Sales tax group", 
    "Withholding tax group", "Release date", "Reversing entry", "Reversing date"
]

# --- 2. PDF PARSING UTILITIES ---

def extract_text_from_pdf(pdf_file):
    """Extracts raw text from an uploaded PDF file for regex parsing."""
    text = ""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        st.error(f"Error reading PDF: {e}")
    return text

def parse_zoho_pdf_data(raw_text):
    # TODO: Implement precise Regular Expressions to find:
    # 1. Customer Name
    # 2. Gross Amount
    # 3. Merchant Fee
    # 4. Refunds / Adjustments
    pass

def parse_stripe_pdf_data(raw_text):
    # TODO: Implement precise Regular Expressions to find Stripe specific details
    pass

# --- 3. CORE PROCESSING FUNCTIONS ---

def process_monthly_expense(row, d365_guide):
    # TODO: Implement Monthly lookup logic
    pass

def process_zoho_payment(row, zoho_pdf_text, d365_guide):
    # TODO: Implement Zoho Gross/Fee split using parsed PDF data
    pass

def process_stripe_payment(row, stripe_pdf_text, d365_guide):
    # TODO: Implement Stripe Gross/Fee split using parsed PDF data
    pass

def process_fallback(row):
    # Implement Fallback / Non-Monthly
    entry = {col: "" for col in D365_COLUMNS}
    entry["Date"] = row.get("Date", "") # Placeholder for actual BOA date col
    entry["Company"] = "bwa"
    entry["Description"] = row.get("Description", "") # Map AS IS
    entry["Debit"] = row.get("Amount", "") # Map AS IS
    entry["Offset company"] = "bwa"
    entry["Bank Account Type"] = "Bank"
    entry["Currency"] = "USD"
    entry["Exchange rate"] = 1.00
    entry["Sales tax group"] = "AVATAX"
    entry["Reversing entry"] = "No"
    
    return entry

# --- 4. MAIN ROUTING ENGINE ---

def generate_journal_entries(boa_df, d365_guide_df, zoho_text, stripe_text):
    journal_entries = []
    
    for index, row in boa_df.iterrows():
        desc = str(row.get('Description', '')).upper()
        
        # Tier 1 & 2 Routing Logic with Corrected Stripe Rule
        if "ZOHO" in desc:
            entries = process_zoho_payment(row, zoho_text, d365_guide_df)
            # journal_entries.extend(entries)
        elif "STRIPE" in desc: 
            entries = process_stripe_payment(row, stripe_text, d365_guide_df)
            # journal_entries.extend(entries)
        elif check_if_monthly(row, d365_guide_df): 
            entries = process_monthly_expense(row, d365_guide_df)
            # journal_entries.extend(entries)
        else:
            entry = process_fallback(row)
            journal_entries.append(entry)
            
    return pd.DataFrame(journal_entries, columns=D365_COLUMNS)

def check_if_monthly(row, guide):
    # TODO: Implement pattern matching against guide
    return False

# --- 5. STREAMLIT UI ---

st.title("📊 D365 Journal Entry Automation")

col1, col2 = st.columns(2)

with col1:
    boa_file = st.file_uploader("1. Upload Bank of America Export (Excel/CSV)", type=["xlsx", "xls", "csv"])
    guide_file = st.file_uploader("2. Upload D365 Journal Guide (Excel)", type=["xlsx", "xls"])

with col2:
    zoho_file = st.file_uploader("3. Upload Zoho Record (PDF)", type=["pdf"])
    stripe_file = st.file_uploader("4. Upload Stripe Record (PDF)", type=["pdf"])

if st.button("Generate D365 Journal Entries", type="primary"):
    if boa_file and guide_file:
        # boa_df = pd.read_excel(boa_file)
        
        # Extract text from PDFs if uploaded
        zoho_text = extract_text_from_pdf(zoho_file) if zoho_file else ""
        stripe_text = extract_text_from_pdf(stripe_file) if stripe_file else ""
        
        st.success("Files processed successfully! (Awaiting final regex and column mappings).")
        
        # Mock Output
        mock_df = pd.DataFrame(columns=D365_COLUMNS)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            mock_df.to_excel(writer, index=False, sheet_name='D365_Upload')
        output.seek(0)
        
        st.download_button(
            label="⬇️ Download D365 Journal Entry Excel",
            data=output,
            file_name="D365_Journal_Entries.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.error("Please upload at least the Bank of America statement and the D365 Journal Guide.")
