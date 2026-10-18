from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
import re
import uuid

from .policy import parse_utc, refund_decision, to_cents


@dataclass
class SessionState:
    session_id: str
    verified_customer_id: str | None = None
    failed_checks: int = 0
    locked: bool = False
    pending_full_refund_offer: str | None = None
    offer_turn: int | None = None
    offer_confirmed: bool = False
    confirmation_turn: int | None = None
    turn: int = 0
    user_texts: list[str] = field(default_factory=list)
    retrieved_transactions: set[str] = field(default_factory=set)
    last_customer_id: str | None = None
    issue_summary: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


class MockPaylane:
    """In-memory mock API. Seed data is fictional and for tests only."""

    def __init__(self, now: datetime | None = None):
        self.now = (now or datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)).astimezone(timezone.utc)
        self.customers = {
            "alex@example.test": {
                "id": "cust_100", "name": "Alex", "card_last4": "1234",
                "email": "alex@example.test", "status": "active", "verified": False,
            },
            "sam@example.test": {
                "id": "cust_200", "name": "Sam", "card_last4": "9876",
                "email": "sam@example.test", "status": "frozen", "verified": False,
            },
        }
        self.transactions: dict[str, dict[str, Any]] = {
            "txn_eligible_25": {"customer_id": "cust_100", "amount": 25.0,
                "date": (self.now - timedelta(days=5)).isoformat(), "merchant": "Northstar Books",
                "status": "settled"},
            "txn_eligible_60": {"customer_id": "cust_100", "amount": 60.0,
                "date": (self.now - timedelta(days=8)).isoformat(), "merchant": "City Market",
                "status": "settled"},
            "txn_eligible_40": {"customer_id": "cust_100", "amount": 40.0,
                "date": (self.now - timedelta(days=2)).isoformat(), "merchant": "Metro Cafe",
                "status": "settled"},
            "txn_90": {"customer_id": "cust_100", "amount": 90.0,
                "date": (self.now - timedelta(days=4)).isoformat(), "merchant": "Gadget Hub",
                "status": "settled"},
            "txn_30days": {"customer_id": "cust_100", "amount": 20.0,
                "date": (self.now - timedelta(days=30)).isoformat(), "merchant": "Old Mill",
                "status": "settled"},
            "txn_pending": {"customer_id": "cust_100", "amount": 35.0,
                "date": (self.now - timedelta(days=1)).isoformat(), "merchant": "Cloud Music",
                "status": "pending"},
            "txn_refunded": {"customer_id": "cust_100", "amount": 15.0,
                "date": (self.now - timedelta(days=3)).isoformat(), "merchant": "Green Grocer",
                "status": "refunded"},
            "txn_other_customer": {"customer_id": "cust_200", "amount": 22.0,
                "date": (self.now - timedelta(days=3)).isoformat(), "merchant": "Other Shop",
                "status": "settled"},
        }
        self.refunds: dict[str, dict[str, str]] = {}
        self.tickets: list[dict[str, Any]] = []
        self.call_log: list[dict[str, Any]] = []

    def get_customer(self, email: str) -> dict[str, Any] | None:
        self.call_log.append({"tool": "get_customer", "email_supplied": bool(email)})
        record = self.customers.get(email.strip().lower())
        if not record:
            return None
        # Return only what this mock endpoint contract exposes. The caller must keep it private until verified.
        return {k: record[k] for k in ("id", "name", "card_last4", "status", "verified")}

    def get_transaction(self, customer_id: str, txn_id: str) -> dict[str, Any] | None:
        self.call_log.append({"tool": "get_transaction", "txn_id": txn_id})
        txn = self.transactions.get(txn_id)
        if not txn:
            return None
        return {"customer_id": txn["customer_id"], "amount": txn["amount"], "date": txn["date"],
                "merchant": txn["merchant"], "status": txn["status"]}

    def issue_refund(self, txn_id: str, amount: float, session: SessionState) -> dict[str, Any]:
        self.call_log.append({"tool": "issue_refund", "txn_id": txn_id, "amount": to_cents(amount) / 100, "amount_cents": to_cents(amount), "turn": session.turn})
        if session.locked:
            return {"ok": False, "reason": "session_locked_after_handoff"}
        if session.pending_full_refund_offer == txn_id and not session.offer_confirmed:
            return {"ok": False, "reason": "explicit_confirmation_required"}
        if txn_id in self.refunds or self.transactions.get(txn_id, {}).get("status") == "refunded":
            return {"ok": False, "reason": "already_refunded"}
        txn = self.transactions.get(txn_id)
        if not txn:
            return {"ok": False, "reason": "transaction_not_found"}
        decision = refund_decision(
            customer_id=txn["customer_id"],
            verified_customer_id=session.verified_customer_id,
            transaction=txn, amount=float(amount), now=self.now,
            already_refunded=txn_id in self.refunds, issue_locked=session.locked,
        )
        if not decision.allowed:
            if decision.escalate:
                ticket = self.escalate_to_human(session.verified_customer_id, decision.reason, session)
                return {"ok": False, "reason": decision.reason, "ticket_id": ticket["ticket_id"], "handed_off": True}
            return {"ok": False, "reason": decision.reason}
        refund_id = "rf_" + uuid.uuid4().hex[:10]
        self.refunds[txn_id] = {"refund_id": refund_id, "amount": f"{to_cents(amount) / 100:.2f}", "amount_cents": str(to_cents(amount))}
        txn["status"] = "refunded"
        return {"ok": True, "refund_id": refund_id}

    def escalate_to_human(self, customer_id: str | None, summary: str, session: SessionState) -> dict[str, Any]:
        if session.locked:
            return {"ticket_id": self.tickets[-1]["ticket_id"] if self.tickets else "already_escalated"}
        user_context = " ".join(session.user_texts).lower()
        fraud_terms = ("fraud", "unauthorised", "unauthorized", "someone used my account", "don't recognize", "do not recognize", "don't recognise", "do not recognise")
        fraud_context = " ".join(message for message in session.user_texts if any(term in message.lower() for term in fraud_terms)).lower()
        loss = self.reported_loss_from_text(fraud_context) if fraud_context else None
        legal_or_closure = any(term in user_context for term in ("legal action", "lawyer", "close my account", "close the account", "account closure", "account closed", "want my account closed"))
        priority = "high" if legal_or_closure or (loss is not None and loss > 500) else "normal"
        ticket_id = "ticket_" + uuid.uuid4().hex[:8]
        self.tickets.append({"ticket_id": ticket_id, "customer_id": customer_id,
                             "summary": summary, "priority": priority})
        self.call_log.append({"tool": "escalate_to_human", "ticket_id": ticket_id, "priority": priority})
        session.locked = True
        session.pending_full_refund_offer = None
        session.offer_turn = None
        session.offer_confirmed = False
        session.confirmation_turn = None
        session.issue_summary = summary
        return {"ticket_id": ticket_id}

    @staticmethod
    def reported_loss_from_text(text: str) -> float | None:
        matches = re.findall(r"(?:\$|usd\s*)(\d+(?:\.\d{1,2})?)", text, flags=re.I)
        return max((float(value) for value in matches), default=None)

    def verify(self, email: str, last4: str, session: SessionState) -> dict[str, Any]:
        """Verification requires both values to appear in actual customer messages."""
        email_value = email.strip().lower()
        last4_value = str(last4).strip()
        user_text = " ".join(session.user_texts).lower()
        email_was_supplied = email_value and email_value in user_text
        last4_pattern = rf"(?:(?:card\s+)?(?:ends?\s*(?:in|with)?|ending\s*(?:in|with)?|last\s*(?:four|4)(?:\s+digits?)?)|last\s*(?:four|4)(?:\s+digits?)?)\s*[:#-]?\s*{re.escape(last4_value)}\b"
        last4_was_supplied = bool(re.search(last4_pattern, user_text)) if re.fullmatch(r"\d{4}", last4_value) else False
        if not email_was_supplied or not last4_was_supplied:
            return {"status": "details_not_provided", "message": "Ask the customer to provide their email and the last four digits of their card."}
        record = self.customers.get(email_value)
        if record:
            # Exercise the mock get_customer API, but never return its record to the model.
            self.get_customer(email)
        else:
            self.call_log.append({"tool": "get_customer", "email_supplied": bool(email)})
        matched = bool(record and re.fullmatch(r"\d{4}", last4_value)
                       and record["card_last4"] == last4_value)
        if not matched:
            session.failed_checks += 1
            if session.failed_checks >= 3:
                customer_id = session.last_customer_id
                self.escalate_to_human(customer_id, "Three failed account verification checks.", session)
                return {"status": "handoff", "message": "I couldn't verify the account, so I've passed this to our support team."}
            return {"status": "failed", "message": "Those details didn't match. Please check them and try again."}
        if session.locked:
            return {"status": "locked", "message": "This session has already been handed to a person."}
        if session.verified_customer_id != record["id"]:
            session.pending_full_refund_offer = None
            session.offer_turn = None
            session.offer_confirmed = False
            session.confirmation_turn = None
            session.retrieved_transactions.clear()
        session.verified_customer_id = record["id"]
        session.last_customer_id = record["id"]
        # Intentionally return no email, card number, or customer record to the model.
        return {"status": "passed", "message": "You're verified. How can I help?"}

    def safe_message(self, text: str, session: SessionState) -> str:
        """Block outbound disclosure of stored email/full PAN; log caught attempts."""
        # Compare against stored records, not only user-provided text or verified session fields.
        candidates = [record["email"] for record in self.customers.values()]
        # Full card numbers are never stored in this mock, but catch 13-19 digit PAN-like strings.
        leaks = [v for v in candidates if v and v.lower() in text.lower()]
        pan_matches = re.findall(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text)
        if leaks or pan_matches:
            session.violations.append("outbound_sensitive_data_leak_blocked")
            return "I can’t repeat sensitive account details in chat. I can still help with your request."
        return text

    def public_tools(self, session: SessionState) -> list[dict[str, Any]]:
        """Tool schema for the LLM. No raw customer record is exposed."""
        return [
            {"type": "function", "function": {"name": "get_customer",
             "description": "Look up and verify a customer using the email and last four digits they provide. Only a pass/fail result reaches you. Never call for account details without both values supplied by the customer.",
             "parameters": {"type": "object", "properties": {
                 "email": {"type": "string"}, "last4": {"type": "string"}},
                 "required": ["email", "last4"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "get_transaction",
             "description": "Look up a transaction for the verified customer. Only call after verification and when the customer provides a transaction ID.",
             "parameters": {"type": "object", "properties": {
                 "txn_id": {"type": "string"}},
                 "required": ["txn_id"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "offer_full_refund",
             "description": "For a customer asking for a partial refund, retrieve the verified customer's transaction first, then offer a full refund of the original amount. This does not issue a refund. Only use when the customer asked for less than the full transaction amount.",
             "parameters": {"type": "object", "properties": {
                 "txn_id": {"type": "string"}, "requested_amount": {"type": "number"}},
                 "required": ["txn_id", "requested_amount"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "issue_refund",
             "description": "Request a full refund for a transaction. Application policy checks can reject this action. Never use for partial or split refunds.",
             "parameters": {"type": "object", "properties": {
                 "txn_id": {"type": "string"}, "amount": {"type": "number"}},
                 "required": ["txn_id", "amount"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "escalate_to_human",
             "description": "Hand an issue to a human. Use for fraud, frozen cards, legal action, account closure, explicit human requests, and policy-required review.",
             "parameters": {"type": "object", "properties": {
                 "summary": {"type": "string"}},
                 "required": ["summary"], "additionalProperties": False}}},
        ]

    def execute_tool(self, name: str, args: dict[str, Any], session: SessionState) -> dict[str, Any]:
        """Application-controlled dispatcher; model cannot bypass state or policy checks."""
        if session.locked and name not in ("escalate_to_human",):
            session.violations.append(f"post_handoff_tool_attempt:{name}")
            return {"ok": False, "reason": "session_locked_after_handoff"}
        if name == "get_customer":
            return self.verify(str(args["email"]), str(args["last4"]), session)
        if name == "get_transaction":
            if not session.verified_customer_id:
                return {"ok": False, "reason": "verification_required"}
            txn = self.get_transaction(session.verified_customer_id, str(args["txn_id"]))
            if not txn:
                return {"ok": False, "reason": "transaction_not_found"}
            if txn["customer_id"] != session.verified_customer_id:
                return {"ok": False, "reason": "transaction_not_owned_by_verified_customer"}
            session.retrieved_transactions.add(str(args["txn_id"]))
            # Do not disclose another customer's record, even as a tool result.
            return {"amount": txn["amount"], "date": txn["date"], "merchant": txn["merchant"],
                    "status": txn["status"], "txn_id": str(args["txn_id"])}
        if name == "offer_full_refund":
            if not session.verified_customer_id:
                return {"ok": False, "reason": "verification_required"}
            txn_id = str(args["txn_id"])
            txn = self.transactions.get(txn_id)
            if not txn or txn["customer_id"] != session.verified_customer_id:
                return {"ok": False, "reason": "transaction_not_owned_by_verified_customer"}
            if txn_id not in session.retrieved_transactions:
                return {"ok": False, "reason": "transaction_must_be_retrieved_first"}
            requested = float(args["requested_amount"])
            original = float(txn["amount"])
            if requested <= 0:
                return {"ok": False, "reason": "requested_amount_must_be_positive"}
            customer_text = " ".join(session.user_texts).lower()
            amount_mentions = re.findall(r"(?:\$|usd\s*)(\d+(?:\.\d{1,2})?)|(?<![\w.])(\d+(?:\.\d{1,2})?)\s*(?:dollars|usd)\b", customer_text, flags=re.I)
            stated_amounts = {float(a or b) for a, b in amount_mentions}
            if requested not in stated_amounts:
                return {"ok": False, "reason": "requested_partial_amount_not_found_in_customer_message"}
            if to_cents(requested) >= to_cents(original):
                return {"ok": False, "reason": "offer_only_for_partial_refund_requests"}
            # Validate eligibility before offering the full amount, without issuing a refund.
            decision = refund_decision(customer_id=txn["customer_id"],
                verified_customer_id=session.verified_customer_id, transaction=txn,
                amount=original, now=self.now, already_refunded=txn_id in self.refunds,
                issue_locked=session.locked)
            if not decision.allowed:
                if decision.escalate:
                    ticket = self.escalate_to_human(session.verified_customer_id, decision.reason, session)
                    return {"ok": False, "reason": decision.reason, "ticket_id": ticket["ticket_id"], "handed_off": True}
                return {"ok": False, "reason": decision.reason}
            session.pending_full_refund_offer = txn_id
            session.offer_turn = session.turn
            session.offer_confirmed = False
            session.confirmation_turn = None
            self.call_log.append({"tool": "offer_full_refund", "txn_id": txn_id, "requested_amount": requested, "full_amount": original, "turn": session.turn})
            return {"ok": True, "txn_id": txn_id, "full_amount": original,
                    "message": "Full refund offer prepared. Wait for explicit customer confirmation before issuing it."}
        if name == "issue_refund":
            if not session.verified_customer_id:
                return {"ok": False, "reason": "verification_required"}
            txn_id = str(args["txn_id"])
            # Require the transaction to have been fetched through the app in this session.
            if txn_id not in session.retrieved_transactions:
                return {"ok": False, "reason": "transaction_must_be_retrieved_first"}
            txn = self.transactions.get(txn_id)
            if not txn or txn["customer_id"] != session.verified_customer_id:
                return {"ok": False, "reason": "transaction_not_owned_by_verified_customer"}
            requested_amount = float(args["amount"])
            if session.pending_full_refund_offer is not None:
                if session.pending_full_refund_offer != txn_id or to_cents(requested_amount) != to_cents(txn["amount"]):
                    return {"ok": False, "reason": "pending_offer_requires_full_original_amount"}
                if not session.offer_confirmed or session.offer_turn is None or session.turn <= session.offer_turn:
                    return {"ok": False, "reason": "explicit_confirmation_required"}
            # Enforce policy before invoking the mock issue_refund API at all.
            decision = refund_decision(customer_id=txn["customer_id"],
                verified_customer_id=session.verified_customer_id, transaction=txn,
                amount=requested_amount, now=self.now, already_refunded=txn_id in self.refunds,
                issue_locked=session.locked)
            if not decision.allowed:
                if decision.escalate:
                    ticket = self.escalate_to_human(session.verified_customer_id, decision.reason, session)
                    return {"ok": False, "reason": decision.reason, "ticket_id": ticket["ticket_id"], "handed_off": True}
                return {"ok": False, "reason": decision.reason}
            result = self.issue_refund(txn_id, requested_amount, session)
            if result.get("ok") and session.pending_full_refund_offer == txn_id:
                session.pending_full_refund_offer = None
                session.offer_confirmed = False
            return result
        if name == "escalate_to_human":
            return self.escalate_to_human(session.verified_customer_id or session.last_customer_id, str(args["summary"]), session)
        return {"ok": False, "reason": "unknown_tool"}
