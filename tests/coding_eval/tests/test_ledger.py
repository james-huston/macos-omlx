"""Hidden oracle tests for the idempotent ledger challenge.

Submissions provide ledger.py on sys.path (see run_eval.py).
"""

from __future__ import annotations

import pytest

from ledger import (
    DuplicateIdempotencyKeyError,
    InsufficientFundsError,
    Ledger,
    LedgerError,
    NotFoundError,
)


def test_create_and_balance():
    led = Ledger()
    led.create_account("a", 100)
    assert led.balance("a") == 100


def test_create_duplicate_and_negative_opening():
    led = Ledger()
    led.create_account("a", 0)
    with pytest.raises(LedgerError):
        led.create_account("a", 1)
    with pytest.raises(LedgerError):
        led.create_account("b", -1)


def test_balance_missing():
    led = Ledger()
    with pytest.raises(NotFoundError):
        led.balance("nope")


def test_deposit_withdraw_happy_path():
    led = Ledger()
    led.create_account("a", 50)
    assert led.deposit("a", 25, idempotency_key="d1") == 75
    assert led.withdraw("a", 10, idempotency_key="w1") == 65


def test_deposit_rejects_non_positive():
    led = Ledger()
    led.create_account("a", 0)
    with pytest.raises(LedgerError):
        led.deposit("a", 0, idempotency_key="d0")
    with pytest.raises(LedgerError):
        led.deposit("a", -5, idempotency_key="dneg")


def test_withdraw_insufficient():
    led = Ledger()
    led.create_account("a", 10)
    with pytest.raises(InsufficientFundsError):
        led.withdraw("a", 11, idempotency_key="w")
    assert led.balance("a") == 10


def test_deposit_idempotent_replay():
    led = Ledger()
    led.create_account("a", 0)
    assert led.deposit("a", 40, idempotency_key="same") == 40
    assert led.deposit("a", 40, idempotency_key="same") == 40
    assert led.balance("a") == 40


def test_idempotency_key_conflict_different_args():
    led = Ledger()
    led.create_account("a", 0)
    led.deposit("a", 10, idempotency_key="k")
    with pytest.raises(DuplicateIdempotencyKeyError):
        led.deposit("a", 11, idempotency_key="k")
    assert led.balance("a") == 10


def test_transfer_happy_and_idempotent():
    led = Ledger()
    led.create_account("a", 100)
    led.create_account("b", 0)
    assert led.transfer("a", "b", 30, idempotency_key="t1") == (70, 30)
    assert led.transfer("a", "b", 30, idempotency_key="t1") == (70, 30)
    assert led.balance("a") == 70
    assert led.balance("b") == 30


def test_transfer_insufficient_is_atomic():
    led = Ledger()
    led.create_account("a", 5)
    led.create_account("b", 1)
    with pytest.raises(InsufficientFundsError):
        led.transfer("a", "b", 6, idempotency_key="t")
    assert led.balance("a") == 5
    assert led.balance("b") == 1


def test_transfer_missing_account():
    led = Ledger()
    led.create_account("a", 50)
    with pytest.raises(NotFoundError):
        led.transfer("a", "missing", 1, idempotency_key="t")


def test_transfer_key_conflict_with_deposit_key_space():
    """Idempotency keys are global to the ledger, not per-method."""
    led = Ledger()
    led.create_account("a", 100)
    led.create_account("b", 0)
    led.deposit("a", 5, idempotency_key="shared")
    with pytest.raises(DuplicateIdempotencyKeyError):
        led.transfer("a", "b", 5, idempotency_key="shared")


def test_failed_withdraw_does_not_consume_idempotency_key():
    """A failed attempt should allow a later success with the same key.

    (Common ledger semantics: only committed ops reserve the key.)
    """
    led = Ledger()
    led.create_account("a", 5)
    with pytest.raises(InsufficientFundsError):
        led.withdraw("a", 10, idempotency_key="w")
    assert led.withdraw("a", 5, idempotency_key="w") == 0
