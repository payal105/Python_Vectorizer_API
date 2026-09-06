"""Credit accounting.

Mirrors the vectorizer.ai billing model: `test` results are free but
watermarked, `preview` costs a fraction of a credit, `production` costs a
full credit. Charges are only committed once a result has been produced, so
a failed job never bills the caller.

The default store keeps balances in memory — fine for a single process and
for development. Implement :class:`CreditStore` against your own database to
persist balances across restarts and workers.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import InsufficientCredits

# Credits charged per successful request, by mode.
COST_BY_MODE: dict[str, float] = {
    "test": 0.00,
    "preview": 0.20,
    "production": 1.00,
}


def cost_for_mode(mode: str) -> float:
    return COST_BY_MODE.get(mode, COST_BY_MODE["production"])


@dataclass(frozen=True, slots=True)
class Receipt:
    receipt_id: str
    key_id: str
    mode: str
    calculated: float
    charged: float
    balance_after: float


@dataclass(slots=True)
class AccountSnapshot:
    key_id: str
    balance: float
    charged_total: float
    requests: int


class CreditStore(Protocol):
    def reserve(self, key_id: str, amount: float) -> None: ...
    def commit(self, key_id: str, mode: str, amount: float) -> Receipt: ...
    def snapshot(self, key_id: str) -> AccountSnapshot: ...


class InMemoryCreditStore:
    """Thread-safe, process-local credit ledger."""

    def __init__(self, default_balance: float, enabled: bool = True) -> None:
        self.default_balance = default_balance
        self.enabled = enabled
        self._balances: dict[str, float] = {}
        self._charged: dict[str, float] = {}
        self._requests: dict[str, int] = {}
        self._lock = threading.Lock()

    def _balance_locked(self, key_id: str) -> float:
        return self._balances.setdefault(key_id, self.default_balance)

    def reserve(self, key_id: str, amount: float) -> None:
        """Fail fast before doing expensive work if the caller cannot pay."""
        if not self.enabled or amount <= 0:
            return
        with self._lock:
            if self._balance_locked(key_id) < amount:
                raise InsufficientCredits(
                    f"This request costs {amount:.2f} credits but the account "
                    f"balance is {self._balance_locked(key_id):.2f}."
                )

    def commit(self, key_id: str, mode: str, amount: float) -> Receipt:
        with self._lock:
            balance = self._balance_locked(key_id)
            charged = amount if self.enabled else 0.0
            if charged > 0:
                if balance < charged:
                    raise InsufficientCredits()
                balance -= charged
                self._balances[key_id] = balance
                self._charged[key_id] = self._charged.get(key_id, 0.0) + charged
            self._requests[key_id] = self._requests.get(key_id, 0) + 1
            return Receipt(
                receipt_id=uuid.uuid4().hex,
                key_id=key_id,
                mode=mode,
                calculated=amount,
                charged=charged,
                balance_after=balance,
            )

    def snapshot(self, key_id: str) -> AccountSnapshot:
        with self._lock:
            return AccountSnapshot(
                key_id=key_id,
                balance=self._balance_locked(key_id),
                charged_total=self._charged.get(key_id, 0.0),
                requests=self._requests.get(key_id, 0),
            )
