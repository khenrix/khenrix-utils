"""Public synthetic wallet/audit lock-order example, deliberately flawed."""
def post_transfer(account_lock, ledger_lock, debit, audit):
    with account_lock:
        with ledger_lock:
            debit()
            audit()

def reverse_transfer(account_lock, ledger_lock, debit, audit):
    with ledger_lock:
        with account_lock:
            audit()
            debit()
