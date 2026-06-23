import pandas as pd
from core.models import ZohoRecord
from typing import List, Any


def clean_numeric_value(val: Any) -> float:
    if pd.isna(val) or val is None:
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    raw = str(val).strip()

    if raw == "":
        return 0.0

    is_negative = False

    # Handle accounting format: ($43.54)
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]

    # Handle trailing minus: 43.54-
    if raw.endswith("-"):
        is_negative = True
        raw = raw[:-1]

    raw = (
        raw.replace("$", "")
        .replace(",", "")
        .replace("−", "-")
        .strip()
    )

    try:
        number = float(raw)
    except ValueError:
        return 0.0

    if is_negative:
        return -abs(number)

    return number


class ZohoParser:
    @staticmethod
    def parse_summary(file_path: str) -> List[ZohoRecord]:
        """
        Parses a Zoho summary export from Excel/CSV.

        Payments and refunds are returned as separate ZohoRecord rows.
        Merchant fees are never calculated from the BOA bank net.
        """

        records: List[ZohoRecord] = []

        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)

        df.columns = [str(c).strip() for c in df.columns]

        cust_col = next(
            (c for c in df.columns if "customer" in c.lower()),
            None
        )

        gross_col = next(
            (
                c for c in df.columns
                if "gross" in c.lower()
                or (
                    "amount" in c.lower()
                    and "net" not in c.lower()
                    and "fee" not in c.lower()
                )
            ),
            None
        )

        fee_col = next(
            (c for c in df.columns if "fee" in c.lower()),
            None
        )

        inv_col = next(
            (c for c in df.columns if "invoice" in c.lower()),
            None
        )

        type_col = next(
            (
                c for c in df.columns
                if "type" in c.lower()
                or "transaction" in c.lower()
            ),
            None
        )

        for _, row in df.iterrows():
            c_name = (
                str(row[cust_col]).strip()
                if cust_col and pd.notna(row[cust_col])
                else None
            )

            inv = (
                str(row[inv_col]).strip()
                if inv_col and pd.notna(row[inv_col])
                else None
            )

            transaction_label = (
                str(row[type_col]).strip().lower()
                if type_col and pd.notna(row[type_col])
                else ""
            )

            gross = clean_numeric_value(row[gross_col]) if gross_col else 0.0

            # Merchant fees may appear as -57.25 in Zoho.
            # Store as positive because D365 debit line should be positive.
            fee = abs(clean_numeric_value(row[fee_col])) if fee_col else 0.0

            is_refund = (
                gross < 0
                or "refund" in transaction_label
            )

            if is_refund:
                refund_amount = abs(gross)

                if refund_amount > 0:
                    records.append(
                        ZohoRecord(
                            customer_name=c_name,
                            gross_amount=0.0,
                            merchant_fee=0.0,
                            refund_amount=refund_amount,
                            invoice_number=inv,
                            transaction_type="refund"
                        )
                    )

                continue

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

        return records
