import pytest

from scheduler.start import _validate_single_scheduler_configuration


def test_scheduler_replica_configuration_guard(monkeypatch):
    monkeypatch.setenv("AFL_SCHEDULER_REPLICAS", "2")
    with pytest.raises(RuntimeError, match="exactly 1"):
        _validate_single_scheduler_configuration()
    monkeypatch.setenv("AFL_SCHEDULER_REPLICAS", "1")
    _validate_single_scheduler_configuration()
