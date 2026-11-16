# Coding challenge: Idempotent ledger

Implement a single Python 3 module named `ledger.py` that provides an in-memory
banking ledger. Your code must be runnable under Python 3.11+ with the standard
library only (no third-party packages).

## Public API

```python
class LedgerError(Exception):
    """Base error for ledger operations."""

class NotFoundError(LedgerError):
    ...

class InsufficientFundsError(LedgerError):
    ...

class DuplicateIdempotencyKeyError(LedgerError):
    ...

class Ledger:
    def create_account(self, account_id: str, opening_balance: int = 0) -> None:
        """Create an account. opening_balance must be >= 0.
        Raise LedgerError if account_id already exists or opening_balance < 0.
        Balances are integer cents.
        """

    def balance(self, account_id: str) -> int:
        """Return balance in cents. Raise NotFoundError if missing."""

    def deposit(self, account_id: str, amount: int, *, idempotency_key: str) -> int:
        """Credit amount (> 0). Return new balance.
        Same idempotency_key + same request args → return prior result (no double credit).
        Same idempotency_key + different args → DuplicateIdempotencyKeyError.
        """

    def withdraw(self, account_id: str, amount: int, *, idempotency_key: str) -> int:
        """Debit amount (> 0). Return new balance.
        Raise InsufficientFundsError if balance would go negative.
        Idempotency rules same as deposit.
        """

    def transfer(
        self,
        from_id: str,
        to_id: str,
        amount: int,
        *,
        idempotency_key: str,
    ) -> tuple[int, int]:
        """Move amount (> 0) from from_id to to_id atomically.
        Return (from_balance, to_balance) after success.
        Raise NotFoundError if either account missing.
        Raise InsufficientFundsError if from_id cannot cover amount.
        Idempotency rules same as deposit (key scoped to the whole transfer).
        """
```

## Requirements

1. All amounts and balances are **non-negative integers** (cents). Reject `amount <= 0`
   with `LedgerError` (except `opening_balance == 0` is allowed on create).
2. Operations that succeed under a given `idempotency_key` must be replay-safe:
   repeating the **exact** same call returns the same result without changing balances again.
3. Reusing an `idempotency_key` for a **different** operation or different arguments must raise
   `DuplicateIdempotencyKeyError`. Idempotency keys are **global** to the ledger (shared across
   deposit / withdraw / transfer).
4. A call that **raises** must **not** consume the idempotency key (a later successful call may
   reuse that key).
5. `transfer` must not leave a partial update if it fails.
6. Do not write tests. Do not print. Do not add a `main`. Only implement the module.
7. Reply with **only** the full contents of `ledger.py` inside one markdown Python code fence.
   No prose before or after the fence.
