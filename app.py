import streamlit as st

st.set_page_config(
    page_title="Manual Payments Journal",
    layout="wide"
)

st.title("Manual Payments Journal Generator")

st.write("""
Upload a payment file and generate D365 journal entries.
This version does not require invoice numbers.
""")

uploaded_file = st.file_uploader(
    "Upload Excel File",
    type=["xlsx"]
)

if uploaded_file:
    st.success("File uploaded successfully")
