import streamlit as st
import pandas as pd
import io
import pdfplumber
import re

# --- 1. CONFIGURATION & CONSTANTS ---
st.set_page_config(page_title="D365 Journal Entry Automation", layout="wide")

# Exact 25-Column D365 Format [cite: 35, 59, 107, 110, 164, 167]
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

def parse_pdf_transaction_data(raw_text, platform="ZOHO"):
    # PLACEHOLDER: We will add the exact Regex here once the text structures are confirmed.
    # Returns a list of dictionaries with: Customer Name, Gross Amount, Merchant Fee
    return [{"customer_name": "Example Customer", "gross_amount": 100.00, "merchant_fee": 3.00}]

# --- 3. D365 ENTRY GENERATORS ---
def create_base_entry(boa_row):
    """Initializes a blank D365 entry with standard defaults."""
    entry = {col: "" for col in D365_COLUMNS}
    entry["Date"] = boa_row.get("Date", "")
    entry["Company"] = "bwa"
    entry["Offset company"] = "bwa"
    entry["Bank Account Type"] = "Bank"
    entry["Currency"] = "USD"
    entry["Exchange rate"] = 1.00
    entry["Sales tax group"] = "AVATAX"
    entry["Reversing entry"] = "No"
    
    # Conditional Offset Routing [cite: 107]
    # NOTE: You will need to map the exact 'Source Account' column from BOA here once known.
    # if source_account == '3371': entry["Offset account"] = "B1000002"
    # elif source_account == '3924': entry["Offset account"] = "B1000003"
    # elif source_account == '3384': entry["Offset account"] = "B1000001"
    
    return entry

def process_payment_batch(boa_row, pdf_text, platform, d365_guide):
    """Handles both Zoho and Stripe Gross/Fee splits[cite: 124, 91]."""
    entries = []
    parsed_data = parse_pdf_transaction_data(pdf_text, platform)
    
    total_fee = 0
    customer_names = []
    
    # 1. Customer Credit Lines [cite: 106, 163]
    for data in parsed_data:
        credit_entry = create_base_entry(boa_row)
        credit_entry["Account name"] = data["customer_name"]
        credit_entry["Account type"] = "Customer"
        # TODO: Lookup Account and Cash Code from Guide using data["customer_name"]
        credit_entry["Posting Profile"] = "AutoPost"
        
        prefix = "MPP " # Example conditional prefix [cite: 108]
        boa_desc = str(boa_row.get("Description", ""))
        credit_entry["Description"] = f"{prefix} {data['customer_name']} _ {boa_desc}"
        credit_entry["Credit"] = data["gross_amount"]
        entries.append(credit_entry)
        
        total_fee += data["merchant_fee"]
        customer_names.append(data["customer_name"])
        
    # 2. Grouped Merchant Fee Debit Line [cite: 94, 150]
    debit_entry = create_base_entry(boa_row)
    debit_entry["Account name"] = "Outside Service (Finance)"
    debit_entry["Account type"] = "Ledger"
    debit_entry["Account"] = "43170111-U26C05001-B735350-UOA003"
    debit_entry["Cash code"] = "OSF005"
    
    fee_prefix = "Stripe Merchant Fee" if platform == "STRIPE" else "Zoho Merchant Fee"
    joined_names = ", ".join(customer_names)
    debit_entry["Description"] = f"{fee_prefix} {joined_names} _ {boa_row.get('Description', '')}"
    debit_entry["Debit"] = total_fee
    
    entries.append(debit_entry)
    return entries

def process_fallback(boa_row):
    """Fallback track for unmapped BOA transactions[cite: 39, 40]."""
    entry = create_base_entry(boa_row)
    entry["Description"] = boa_row.get("Description", "")  # Raw description [cite: 56]
    entry["Debit"] = boa_row.get("Amount", "")            # Exact amount [cite: 57]
    return entry

# --- 4. MAIN ROUTING ENGINE ---
def generate_journal_entries(boa_df, d365_guide_df, zoho_text, stripe_text):
    journal_entries = []
    
    for index, boa_row in boa_df.iterrows():
        desc = str(boa_row.get("Description", "")).upper()
        
        if "ZOHO" in desc:
            journal_entries.extend(process_payment_batch(boa_row, zoho_text, "ZOHO", d365_guide_df))
        elif "STRIPE" in desc: 
            journal_entries.extend(process_payment_batch(boa_row, stripe_text, "STRIPE", d365_guide_df))
        else:
            # Monthly rules go here, defaulting to fallback if no match [cite: 45, 49]
            journal_entries.append(process_fallback(boa_row))
            
    return pd.DataFrame(journal_entries, columns=D365_COLUMNS)

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
        # Load structured data
        boa_df = pd.read_csv(boa_file) if boa_file.name.endswith('.csv') else pd.read_excel(boa_file)
        # guide_df = pd.read_excel(guide_file, sheet_name=None)
        
        # Extract raw text from PDFs
        zoho_text = extract_text_from_pdf(zoho_file) if zoho_file else ""
        stripe_text = extract_text_from_pdf(stripe_file) if stripe_file else ""
        
        # Process Data (Passing None for guide_df until lookups are finalized)
        final_df = generate_journal_entries(boa_df, None, zoho_text, stripe_text)
        
        st.success("✅ Journal entries generated successfully!")
        st.dataframe(final_df) # Show preview in UI
        
        # Prepare Download
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            final_df.to_excel(writer, index=False, sheet_name='D365_Upload')
        output.seek(0)
        
        st.download_button(
            label="⬇️ Download D365 Excel Ready File",
            data=output,
            file_name="D365_Journal_Entries.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.error("Please upload at least the Bank of America statement and the D365 Journal Guide.")
