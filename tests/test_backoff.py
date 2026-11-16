from camviewer.stream_ingest import backoff_delay


def test_backoff_sequence_and_cap():
    assert [backoff_delay(i) for i in range(6)] == [0.5, 1.0, 2.0, 5.0, 5.0, 5.0]
    assert backoff_delay(0, offline=True) == 2.0
    assert backoff_delay(99, offline=True) == 60.0
