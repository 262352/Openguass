from app.multi_agent.paired_decision import decide_paired


def test_paired_decision_has_accept_reject_and_unresolved_states():
    assert decide_paired([.08, .10, .09], delta_min=.03).verdict == "accept"
    assert decide_paired([-.08, -.10, -.09], delta_min=.03).verdict == "reject_harmful"
    assert decide_paired([-.01, 0.0, .01], delta_min=.03).verdict == "reject_equivalent"
    assert decide_paired([-.08, .10, .01], delta_min=.03).verdict == "unresolved"
