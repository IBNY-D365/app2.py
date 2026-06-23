import pandas as pd
from typing import List, Dict, Any
from core.models import ProcessingBatch, BOARecord, ZohoRecord, AccountMasterItem
from core.validators import EngineValidator
from config.mappings import CASH_CODE_MAPPING, OFFSET_ACCOUNT_ROUTING, D365_TEMPLATE_COLUMNS

class D365AutomationEngine:
    def __init__(self, masterlist_path: str):
        self.masterlist = self._load_masterlist(masterlist_path)

    def _load_masterlist(self, path: str) -> Dict[str, AccountMasterItem]:
        df = pd.read_excel(path)
        master_dict = {}
        for _, row in df.iterrows():
            name_key = str(row['Account Name']).strip().lower()
            master_dict[name_key] = AccountMasterItem(
                account_number=str(row['Account #']),
                account_name=str(row['Account Name']),
                payment_term=str(row.get('Payment Term', 'due-on-receipt')).strip().lower()
            )
        return master_dict

    def resolve_account(self, name: str) -> AccountMasterItem:
        """Rule 3.2: String Normalization and master list lookup logic."""
        if not name:
            raise ValueError("Missing customer identifier name.")
        key = name.strip().lower()
        if key in self.masterlist:
            return self.masterlist[key]
        raise ValueError(f"Customer '{name}' not found inside Account Masterlist reference.")

    def process_transaction_group(self, boa_rec: BOARecord, zoho_recs: List[ZohoRecord]) -> tuple[List[Dict[str, Any]], List[str]]:
        batch = ProcessingBatch(boa_record=boa_rec, zoho_records=zoho_recs)
        errors = EngineValidator.validate_batch_invariants(batch)
        
        if errors:
            return [], errors

        journal_lines = []
        processed_accounts = []
        total_grouped_fee = 0.0
        
        offset_acct = OFFSET_ACCOUNT_ROUTING.get(boa_rec.source_account, "")
        if not offset_acct:
            errors.append(f"Invalid BOA Source Account Routing code: {boa_rec.source_account}")
            return [], errors

        # -------------------------------------------------------------
        # STEP 1: CREDIT LINE GENERATION (Customer Payment Segment)
        # -------------------------------------------------------------
        for z_rec in zoho_recs:
            try:
                master_item = self.resolve_account(z_rec.customer_name)
            except ValueError as e:
                errors.append(str(e))
                continue
                
            processed_accounts.append(master_item)
            total_grouped_fee += z_rec.merchant_fee
            
            # Cash Code determination pipeline
            term_info = CASH_CODE_MAPPING.get(master_item.payment_term, CASH_CODE_MAPPING['fallback'])
            cash_code = term_info[0]
            
            # Prefix execution check
            prefix = "MPP " if cash_code == "AR002" else ""
            desc = f"{prefix}{master_item.account_number} {master_item.account_name}_{boa_rec.description}"
            
            credit_line = {
                "Date": boa_rec.date,
                "Voucher": "",  # Rule: Leave BLANK
                "Account name": master_item.account_name,
                "Company": "bwa",
                "Account type": "Customer",
                "Account": master_item.account_number,
                "Posting Profile": "AutoPost",
                "Cash code": cash_code,
                "Description": desc,
                "Debit": "",
                "Credit": z_rec.gross_amount,
                "Item sales tax group": "",
                "Sales tax code": "",
                "Offset company": "bwa",
                "Bank Account Type": "Bank",
                "Offset account": offset_acct,
                "Offset transaction text": "",
                "Currency": "USD",
                "Exchange rate": 1.00,
                "Item sales tax group2": "",
                "Sales tax group": "AVATAX",
                "Withholding tax group": "",
                "Release date": "",
                "Reversing entry": "No",
                "Reversing date": ""
            }
            journal_lines.append(credit_line)

        if errors:
            return [], errors

        # -------------------------------------------------------------
        # STEP 2: DEBIT LINE GENERATION (Zoho Merchant Fee Segment)
        # -------------------------------------------------------------
        if total_grouped_fee > 0:
            # Rule 3.3: Dynamic concatenation handling for Single vs Multi-Payment context strings
            if len(processed_accounts) == 1:
                acc = processed_accounts[0]
                fee_desc = f"Zoho Merchant Fee {acc.account_number} {acc.account_name}_{boa_rec.description}"
            else:
                account_strings = ", ".join([f"{a.account_number} {a.account_name}" for a in processed_accounts])
                fee_desc = f"Zoho Merchant Fee {account_strings}_{boa_rec.description}"
                
            debit_line = {
                "Date": boa_rec.date,
                "Voucher": "",
                "Account name": "Outside Service (Finance)",
                "Company": "bwa",
                "Account type": "Ledger",
                "Account": "43170111-U26C05001-B735350-UOA003",
                "Posting Profile": "",
                "Cash code": "OSF005",
                "Description": fee_desc,
                "Debit": total_grouped_fee,
                "Credit": "",
                "Item sales tax group": "",
                "Sales tax code": "",
                "Offset company": "bwa",
                "Bank Account Type": "Bank",
                "Offset account": offset_acct,
                "Offset transaction text": "",
                "Currency": "USD",
                "Exchange rate": 1.00,
                "Item sales tax group2": "",
                "Sales tax group": "AVATAX",
                "Withholding tax group": "",
                "Release date": "",
                "Reversing entry": "No",
                "Reversing date": ""
            }
            journal_lines.append(debit_line)

        return journal_lines, errors

