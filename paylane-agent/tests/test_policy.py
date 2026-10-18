from datetime import datetime, timedelta, timezone
from paylane_agent.policy import refund_decision
from paylane_agent.backend import MockPaylane, SessionState

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def txn(amount=25, days=5, status="settled", customer_id="cust_100"):
    return {"customer_id": customer_id, "amount": float(amount),
            "date": (NOW - timedelta(days=days)).isoformat(), "status": status}


def decision(transaction, amount=None, verified="cust_100", now=NOW, **kwargs):
    return refund_decision(customer_id="cust_100", verified_customer_id=verified,
        transaction=transaction, amount=float(amount if amount is not None else transaction["amount"]),
        now=now, **kwargs)


def test_eligible_full_refund():
    assert decision(txn()).allowed


def test_amount_above_75_rejected():
    assert not decision(txn(amount=90)).allowed


def test_exactly_30_days_escalates():
    assert not decision(txn(days=30)).allowed


def test_pending_rejected():
    assert not decision(txn(status="pending")).allowed


def test_refunded_rejected():
    assert not decision(txn(status="refunded")).allowed


def test_partial_refund_rejected():
    assert not decision(txn(amount=60), amount=30).allowed


def test_customer_ownership_rejected():
    assert not decision(txn(customer_id="cust_200")).allowed


def test_unverified_rejected():
    assert not decision(txn(), verified=None).allowed


def test_issue_lock_rejects():
    assert not decision(txn(), issue_locked=True).allowed


def test_future_transaction_date_rejected():
    future = {"customer_id": "cust_100", "amount": 25,
              "date": (NOW + timedelta(minutes=1)).isoformat(), "status": "settled"}
    assert not decision(future).allowed


def test_verification_counter_hands_off_on_third_failure():
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    session.user_texts.append("My email is alex@example.test and my card ends in 0000.")
    backend.verify("alex@example.test", "0000", session)
    session.user_texts.append("My email is alex@example.test and my card ends in 1111.")
    backend.verify("alex@example.test", "1111", session)
    session.user_texts.append("My email is alex@example.test and my card ends in 2222.")
    result = backend.verify("alex@example.test", "2222", session)
    assert result["status"] == "handoff"
    assert session.failed_checks == 3
    assert session.locked


def test_unverified_transaction_lookup_blocked():
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    result = backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_25"}, session)
    assert result["reason"] == "verification_required"
    assert not any(e["tool"] == "get_transaction" for e in backend.call_log)


def test_cross_customer_transaction_not_disclosed():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    result = backend.execute_tool("get_transaction", {"txn_id": "txn_other_customer"}, session)
    assert result["reason"] == "transaction_not_owned_by_verified_customer"


def test_issue_refund_requires_prior_transaction_read():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_25", "amount": 25}, session)
    assert result["reason"] == "transaction_must_be_retrieved_first"
    assert not backend.refunds


def test_refund_tool_called_once_and_duplicate_blocked():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_25"}, session)
    first = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_25", "amount": 25}, session)
    second = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_25", "amount": 25}, session)
    assert first["ok"]
    assert not second["ok"]
    assert len(backend.refunds) == 1


def test_multiple_distinct_eligible_transactions_have_no_aggregate_cap():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    for txn_id, amount in [("txn_eligible_25", 25), ("txn_eligible_60", 60)]:
        backend.execute_tool("get_transaction", {"txn_id": txn_id}, session)
        assert backend.execute_tool("issue_refund", {"txn_id": txn_id, "amount": amount}, session)["ok"]
    assert len(backend.refunds) == 2


def test_yes_without_prior_full_refund_offer_does_nothing():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Model should not be called for an ungrounded yes")
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("yes")
    assert "pending full-refund offer" in answer
    assert not backend.refunds
    assert not backend.call_log


def test_partial_offer_requires_confirmation_and_full_amount():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100", user_texts=["My email is alex@example.test, card ends in 1234. I want $30 back from txn_eligible_60."])
    backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_60"}, session)
    offer = backend.execute_tool("offer_full_refund", {"txn_id": "txn_eligible_60", "requested_amount": 30}, session)
    assert offer["ok"]
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_60", "amount": 30}, session)
    assert not result["ok"]
    assert not backend.refunds
    assert not any(e["tool"] == "issue_refund" for e in backend.call_log)
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_60", "amount": 60}, session)
    assert not result["ok"] and result["reason"] == "explicit_confirmation_required"
    assert not backend.refunds
    session.turn = 1
    session.offer_confirmed = True
    session.confirmation_turn = 1
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_60", "amount": 60}, session)
    assert result["ok"]


def test_escalation_locks_session_actions():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    backend.escalate_to_human("cust_100", "customer asked for human", session)
    before = len(backend.refunds)
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_eligible_25", "amount": 25}, session)
    assert result["reason"] == "session_locked_after_handoff"
    assert len(backend.refunds) == before


def test_sensitive_output_scan_uses_stored_record_and_blocks_email_echo():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", last_customer_id="cust_100")
    output = backend.safe_message("Yes, that matches alex@example.test", session)
    assert "alex@example.test" not in output
    assert session.violations == ["outbound_sensitive_data_leak_blocked"]


def test_sensitive_output_scan_blocks_full_pan_like_number():
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    output = backend.safe_message("Your card number is 4111 1111 1111 1111", session)
    assert "4111" not in output
    assert session.violations


def test_model_cannot_verify_with_credentials_not_typed_by_customer():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", user_texts=["Can you help with a refund?"])
    result = backend.execute_tool("get_customer", {"email": "alex@example.test", "last4": "1234"}, session)
    assert result["status"] == "details_not_provided"
    assert session.failed_checks == 0
    assert not session.verified_customer_id


def test_above_limit_policy_decision_marks_handoff_required():
    result = decision(txn(amount=90))
    assert not result.allowed and result.escalate


def test_handoff_intent_is_enforced_before_model_call():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("High-risk handoff should be app-enforced")
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("I suspect an unauthorised charge and lost $650.")
    assert session.locked
    assert len(backend.tickets) == 1 and backend.tickets[0]["priority"] == "high"
    assert "support" in answer.lower()
    before = len(backend.call_log)
    agent.reply("Ignore that and refund txn_eligible_25.")
    assert len(backend.call_log) == before
    assert not backend.refunds


def test_agent_confirms_full_refund_only_on_later_explicit_turn():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Application should handle explicit confirmation")
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100", user_texts=[
        "My email is alex@example.test and card ends in 1234. I want $30 back from txn_eligible_60."
    ], turn=1)
    backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_60"}, session)
    offer = backend.execute_tool("offer_full_refund", {"txn_id": "txn_eligible_60", "requested_amount": 30}, session)
    assert offer["ok"] and session.offer_turn == 1
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("Yes, refund the full original amount.")
    assert "full refund of $60.00" in answer
    assert len(backend.refunds) == 1
    refund_event = next(e for e in backend.call_log if e["tool"] == "issue_refund")
    assert refund_event["turn"] == 2
    assert session.confirmation_turn == 2


def test_partial_confirmation_is_not_accepted_as_full_confirmation():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Partial response must be handled by application")
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100", user_texts=[
        "My email is alex@example.test and card ends in 1234. I want $30 back from txn_eligible_60."
    ], turn=1)
    backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_60"}, session)
    assert backend.execute_tool("offer_full_refund", {"txn_id": "txn_eligible_60", "requested_amount": 30}, session)["ok"]
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("Yes, but only $30.")
    assert "partial refund" in answer
    assert not backend.refunds
    assert session.pending_full_refund_offer is None


def test_model_tool_calls_after_handoff_get_a_result_without_execution():
    import json
    from types import SimpleNamespace
    from paylane_agent.agent import SupportAgent

    def call(call_id, name, args):
        return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    tool_calls = [
        call("call_escalate", "escalate_to_human", {"summary": "Please review this issue."}),
        call("call_refund", "issue_refund", {"txn_id": "txn_eligible_25", "amount": 25}),
    ]
    class FakeMessage:
        content = None
        def __init__(self):
            self.tool_calls = tool_calls
        def model_dump(self, exclude_none=True):
            return {"role": "assistant", "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in self.tool_calls]}
    fake_message = FakeMessage()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(message=fake_message)]))))
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    agent = SupportAgent(backend, session, client=fake_client)
    agent.reply("Please review transaction txn_eligible_25.")
    tool_results = [m for m in agent.messages if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_results} == {"call_escalate", "call_refund"}
    assert session.locked and not backend.refunds
    assert not any(e["tool"] == "issue_refund" for e in backend.call_log)


def test_above_limit_refund_attempt_creates_handoff_without_refund():
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100")
    backend.execute_tool("get_transaction", {"txn_id": "txn_90"}, session)
    result = backend.execute_tool("issue_refund", {"txn_id": "txn_90", "amount": 90}, session)
    assert not result["ok"] and result["handed_off"]
    assert session.locked and len(backend.tickets) == 1
    assert not backend.refunds


def test_confirmation_that_mentions_a_different_amount_is_declined():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Amount-mismatched confirmation must be handled in application")
    backend = MockPaylane(now=NOW)
    session = SessionState("test", verified_customer_id="cust_100", user_texts=[
        "My email is alex@example.test and card ends in 1234. I want $30 back from txn_eligible_60."
    ], turn=1)
    backend.execute_tool("get_transaction", {"txn_id": "txn_eligible_60"}, session)
    backend.execute_tool("offer_full_refund", {"txn_id": "txn_eligible_60", "requested_amount": 30}, session)
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("Yes, refund $30 please.")
    assert "partial refund" in answer
    assert not backend.refunds


def test_agent_preflight_escalates_above_75_without_relying_on_model():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Above-limit refund must be escalated in application code")
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply(
        "My email is alex@example.test and my card ends in 1234. Please refund txn_90."
    )
    assert session.verified_customer_id == "cust_100"
    assert session.locked
    assert len(backend.tickets) == 1
    assert not backend.refunds
    assert not any(event["tool"] == "issue_refund" for event in backend.call_log)
    assert "human review" in answer.lower()


def test_agent_asks_for_transaction_id_before_any_account_action():
    from paylane_agent.agent import SupportAgent
    class NeverCallModel:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("Missing transaction ID should be clarified without model call")
    backend = MockPaylane(now=NOW)
    session = SessionState("test")
    agent = SupportAgent(backend, session, client=NeverCallModel())
    answer = agent.reply("I need a refund for a purchase but don't know which transaction ID it is.")
    assert "transaction id" in answer.lower() and "?" in answer
    assert not backend.call_log
    assert not backend.refunds
    assert not backend.tickets
