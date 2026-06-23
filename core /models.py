from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date

class BOARecord(BaseModel):
    date: date
    description: str
    net_amount: float
    source_account: str

class ZohoRecord(BaseModel):
    customer_name: Optional[str] = None
    gross_amount: float = 0.0
    merchant_fee: float = 0.0
    refund_amount: float = 0.0
    invoice_number: Optional[str] = None
    fallback_personal_name: Optional[str] = None
    transaction_type: str = "payment"   # "payment" or "refund"

class AccountMasterItem(BaseModel):
    account_number: str
    account_name: str
    payment_term: str

class ProcessingBatch(BaseModel):
    boa_record: BOARecord
    zoho_records: List[ZohoRecord]
    errors: List[str] = Field(default_factory=list)
