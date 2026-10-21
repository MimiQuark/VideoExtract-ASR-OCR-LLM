import extract


def test_fmt_ts_srt_carries_seconds_and_minutes():
    assert extract.fmt_ts(59.9996, True) == "00:01:00,000"
    assert extract.fmt_ts(3599.9996, True) == "01:00:00,000"
    assert extract.fmt_ts(0, True) == "00:00:00,000"


def test_fmt_ts_plain_carries():
    assert extract.fmt_ts(59.9996) == "00:01:00"
    assert extract.fmt_ts(3599.9996) == "01:00:00"
