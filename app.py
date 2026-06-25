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
    if pdf_file is not None:
        try:
            with pdfplumber.open(pdf_file) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
        except Exception as e:
            st.error(f"Error reading PDF {pdf_file.name}: {e}")
    return text

def parse_invoice_receipt(raw_text):
    """Parses standard Payment Receipts to extract the Bill To name."""
    name_match = re.search(r"Bill To\s*\n(.*?)\n", raw_text, re.IGNORECASE)
    customer_name = name_match.group(1).strip() if name_match else "Unknown Customer (Needs Manual Entry)"
    return customer_name

def parse_payout_summary(raw_text):
    """Parses the Zoho/Stripe Payout Summary for the gross, fees, refunds, and adjustments."""
    # Target the exact headers from the Payout Summary screenshot
    gross_match = re.search(r"Funds from Sales\s*\$?([\d,\.]+)", raw_text, re.IGNORECASE)
    fee_match = re.search(r"(?:Zoho Payments Fees|Stripe Fees)\s*-\$?([\d,\.]+)", raw_text, re.IGNORECASE)
    refund_match = re.search(r"Refunds\s*-?\$?([\d,\.]+)", raw_text, re.IGNORECASE)
    adj_match = re.search(r"Adjustments\s*-?\$?([\d,\.]+)", raw_text, re.IGNORECASE)
    
    gross_amount = float(gross_match.group(1).replace(',', '')) if gross_match else 0.00
    merchant_fee = float(fee_match.group(1).replace(',', '')) if fee_match else 0.00
    refunds = float(refund_match.group(1).replace(',', '')) if refund_match else 0.00
    adjustments = float(adj_match.group(1).replace(',', '')) if adj_match else 0.00
    
    return gross_amount, merchant_fee, refunds, adjustments

def extract_transaction_data(payout_text, invoice_text, platform="ZOHO"):
    """Synthesizes data from the payout document and the invoice document."""
    gross, fee, refunds, adjustments = parse_payout_summary(payout_text)
    
    # Missing Name Rule: Use invoice text to find the customer name if payout text lacks it
    customer_name = parse_invoice_receipt(invoice_text) if invoice_text else "Unknown Customer"
    
    return [{
        "customer_name": customer_name,
        "gross_amount": gross,
        "merchant_fee": fee,
        "refunds": refunds,
        "adjustments": adjustments
    }]

# --- 3. D365 ENTRY GENERATORS ---
def
