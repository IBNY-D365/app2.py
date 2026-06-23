def extract_invoice_metadata_intelligent(pdf_file) -> Dict[str, Any]:
    """Scans the invoice to capture the precise Paid Amount and business entity."""
    result = {"customer_name": None, "invoice_number": None, "gross_amount": 0.0, "fallback_personal_name": None}
    try:
        reader = PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        full_text_clean = " ".join(full_text.split())
        
        # 1. Invoice Number Extraction
        inv_num = pdf_file.name.replace(".pdf", "")
        inv_match = re.search(r"(INV-\d+)", full_text_clean, re.IGNORECASE)
        result["invoice_number"] = inv_match.group(1).strip() if inv_match else inv_num
        
        # 2. Source of Truth Gross Amount Extraction
        pm_match = re.search(r"Payment\s*Made[^\d\$]*\$?([0-9,]+\.\d{2})", full_text_clean, re.IGNORECASE)
        if pm_match:
            result["gross_amount"] = clean_numeric_value(pm_match.group(1))
        else:
            totals = re.findall(r"Total[^\d\$]*\$?([0-9,]+\.\d{2})", full_text_clean, re.IGNORECASE)
            if totals:
                result["gross_amount"] = clean_numeric_value(totals[-1])
            else:
                all_decimals = [clean_numeric_value(n) for n in re.findall(r"\b\d+(?:,\d{3})*\.\d{2}\b", full_text_clean)]
                result["gross_amount"] = max(all_decimals) if all_decimals else 0.0
        
        # 3. Robust Customer Name Extraction (UPGRADED)
        
        # Strategy A: Explicit "Customer Name" Field
        cust_match = re.search(r"Customer\s*Name[\s\:]*([A-Za-z0-9\s\.\,\&\-]+?)(?:\s+(?:Invoice|Date|Amount|Terms|Bill\s*To|Ship\s*To|$))", full_text_clean, re.IGNORECASE)
        if cust_match:
            result["customer_name"] = cust_match.group(1).strip()
            
        # Strategy B: "Bill To" Field Fallback
        bill_to_match = re.search(r"Bill\s+To\s*([A-Za-z0-9\s\.\,\-]+?)(?:\s*\d|\s*Ship\s*To|$)", full_text_clean, re.IGNORECASE)
        if bill_to_match:
            result["fallback_personal_name"] = bill_to_match.group(1).strip()
            # If Strategy A failed, use Strategy B as the main name
            if not result["customer_name"]:
                result["customer_name"] = result["fallback_personal_name"]
                
        # Strategy C: Legacy Deep Target Search (InBody Item format)
        if not result["customer_name"]:
            biz_matches = re.findall(r"InBody\d*\s*-\s*[^-]+?-\s*([A-Za-z0-9\s\.\,\&]+)", full_text_clean, re.IGNORECASE)
            for match in biz_matches:
                candidate = re.sub(r'\d+\.\d{2}.*', '', match).strip()
                if candidate and not any(k in candidate.lower() for k in ["malfunction", "check required", "sku", "labor", "board", "cable", "loaner"]):
                    result["customer_name"] = candidate
                    break
                    
    except Exception as e:
        st.error(f"Error executing intelligent metadata capture: {e}")
    return result
