from paylane_agent.scenarios import SCENARIOS


def test_twelve_scenarios_exist_and_have_expected_fields():
    assert len(SCENARIOS) == 12
    assert len({s["id"] for s in SCENARIOS}) == 12
    for scenario in SCENARIOS:
        assert scenario["messages"]
        assert scenario["expect"]
        assert scenario["purpose"]


def test_required_edge_cases_are_covered():
    ids = {s["id"] for s in SCENARIOS}
    assert "09_prompt_injection" in ids
    assert "12_partial_refund_and_confirmation" in ids
