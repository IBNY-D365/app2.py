from core.models import ProcessingBatch

class EngineValidator:
    @staticmethod
    def validate_batch_invariants(batch: ProcessingBatch) -> List[str]:
        """
        Rule 3.3 Balance Invariant Check: 
        Total Gross Amounts - Associated Merchant Fees == Net Amount in BOA Record
        """
        errors = []
        total_gross = sum(z.gross_amount for z in batch.zoho_records)
        total_fees = sum(z.merchant_fee for z in batch.zoho_records)
        calculated_net = total_gross - total_fees
        
        # Checking floating-point operations within standard 2-decimal accuracy margin
        if abs(calculated_net - batch.boa_record.net_amount) > 0.01:
            errors.append(
                f"Mathematical Invariant Failure! BOA Net: {batch.boa_record.net_amount}, "
                f"Calculated Net: {calculated_net:.2f} (Gross: {total_gross}, Fees: {total_fees})"
            )
        return errors
