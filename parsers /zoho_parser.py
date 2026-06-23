gross = clean_numeric_value(row[gross_col]) if gross_col else 0.0
fee = clean_numeric_value(row[fee_col]) if fee_col else 0.0

# Refund row
if gross < 0:
    records.append(
        ZohoRecord(
            customer_name=c_name,
            refund_amount=abs(gross),
            merchant_fee=0.0,
            gross_amount=0.0,
            invoice_number=inv,
            transaction_type="refund"
        )
    )
    continue

# Payment row
if gross > 0:
    records.append(
        ZohoRecord(
            customer_name=c_name,
            gross_amount=gross,
            merchant_fee=fee,
            refund_amount=0.0,
            invoice_number=inv,
            transaction_type="payment"
        )
    )
