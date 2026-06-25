import streamlit as st
import pandas as pd
import io
import pdfplumber
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

def parse_invoice_receipt(raw_text):
    """Parses standard InBody BWA Payment Receipts to extract the Bill To name."""
    # Regex to find the text between "Bill To" and "Payment Date" or similar markers
    name_match = re.search(r"Bill To\s*\n(.*?)\n", raw_text, re.IGNORECASE)
    amount_match = re.search(r"Amount Received\s*\$([\d,\.]+)", raw_text, re.IGNORECASE)
    
    customer_name = name_match.group(1).strip() if name_match else "Unknown Customer"
    gross_amount = float(amount_match.group(1).replace(',', '')) if amount_match else 0.00
    
    return customer_name, gross_amount

def parse_pdf_transaction_data(raw_text, platform="ZOHO"):
    """
    Core parsing engine. Currently defaults to dummy fee data until 
    the Payout Summary format is mapped.
    """
    customer_name, gross_amount = parse_invoice_receipt(raw_text)
    
    # Placeholder: We still need the Payout Summary to get the actual fee!
    return [{
        "customer_name": customer_name, 
        "gross_amount": gross_amount, 
        "merchant_fee": 0.00 # Pending real fee logic
    }]

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
    return entry

def process_payment_batch(boa_row, pdf_text, platform, d365_guide):
    """Handles Gross/Fee splits and generates corresponding lines."""
    entries = []
    parsed_data = parse_pdf_transaction_data(pdf_text, platform)
    
    total_fee = 0
    customer_names = []
    
    for data in parsed_data:
        credit_entry = create_base_entry(boa_row)
        credit_entry["Account name"] = data["customer_name"]
        credit_entry["Account type"] = "Customer"
        credit_entry["Posting Profile"] = "AutoPost"
        
        boa_desc = str(boa_row.get("Description", ""))
        credit_entry["Description"] = f"{data['customer_name']} _ {boa_desc}"
        credit_entry["Credit"] = data["gross_amount"]
        entries.append(credit_entry)
        
        total_fee += data["merchant_fee"]
        customer_names.append(data["customer_name"])
        
    if total_fee > 0:
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
    """Fallback track for unmapped transactions."""
    entry = create_base_entry(boa_row)
    entry["Description"] = boa_row.get("Description", "") 
    entry["Debit"] = boa_row.get("Amount", "")            
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
        boa_df = pd.read_csv(boa_file) if boa_file.name.endswith('.csv') else pd.read_excel(boa_file)
        
        zoho_text = extract_text_from_pdf(zoho_file) if zoho_file else ""
        stripe_text = extract_text_from_pdf(stripe_file) if stripe_file else ""
        
        final_df = generate_journal_entries(boa_df, None, zoho_text, stripe_text)
        
        st.success("✅ Journal entries generated successfully!")
        st.dataframe(final_df) 
        
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
