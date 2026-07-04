def test_builder_interval_default():
    from app.config import get_settings
    assert get_settings().builder_monitor_interval_seconds == 60
