from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    escalate: bool = False


def to_cents(value: float | int | str) -> int:
    """Convert a USD amount to integer cents without binary-float comparisons."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamps must include a timezone.")
    return dt.astimezone(timezone.utc)


def refund_decision(
    *,
    customer_id: str,
    verified_customer_id: str | None,
    transaction: dict[str, Any] | None,
    amount: float,
    now: datetime,
    already_refunded: bool = False,
    issue_locked: bool = False,
) -> PolicyDecision:
    """Pure policy function. Time is injected so boundary tests are deterministic."""
    if issue_locked:
        return PolicyDecision(False, "This issue has already been handed to a person.")
    if not verified_customer_id or verified_customer_id != customer_id:
        return PolicyDecision(False, "The customer is not verified for this account.")
    if not transaction:
        return PolicyDecision(False, "The transaction could not be found.")
    if transaction.get("customer_id") != verified_customer_id:
        return PolicyDecision(False, "The transaction does not belong to the verified customer.")
    if transaction.get("status") == "pending":
        return PolicyDecision(False, "Pending transactions cannot be refunded.")
    if transaction.get("status") == "refunded" or already_refunded:
        return PolicyDecision(False, "This transaction has already been refunded.")
    if transaction.get("status") != "settled":
        return PolicyDecision(False, "Only settled transactions can be refunded.")
    original_amount = to_cents(transaction["amount"])
    if original_amount > 7500:
        return PolicyDecision(False, "Transactions above $75 require a human review.", escalate=True)
    if to_cents(amount) != original_amount:
        return PolicyDecision(False, "Only a full refund of the original transaction is supported.")
    age_seconds = (now.astimezone(timezone.utc) - parse_utc(transaction["date"])).total_seconds()
    if age_seconds < 0:
        return PolicyDecision(False, "The transaction date is in the future and needs review.")
    if age_seconds >= 30 * 24 * 60 * 60:
        return PolicyDecision(False, "Transactions 30 days old or older require a human review.", escalate=True)
    return PolicyDecision(True, "Eligible for a full refund.")


