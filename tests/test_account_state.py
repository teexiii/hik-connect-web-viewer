from camviewer.account_state import AccountState


def test_epoch_persists_and_bumps(tmp_path):
    state = AccountState(tmp_path)
    assert state.epoch == 1
    state.bump_epoch()
    assert AccountState(tmp_path).epoch == 2


def test_breaker_trips_after_three_failures(tmp_path):
    state = AccountState(tmp_path)
    state.mark_ok()
    assert [state.record_auth_failure() for _ in range(3)] == [False, False, True]
    assert state.status == "needs_attention" and state.reason == "auth" and not state.usable
