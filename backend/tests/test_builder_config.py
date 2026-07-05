def test_builder_interval_default():
    from app.config import get_settings
    assert get_settings().builder_monitor_interval_seconds == 60


def test_vix_wait_poll_default():
    from app.config import get_settings
    assert get_settings().vix_wait_poll_seconds == 5
