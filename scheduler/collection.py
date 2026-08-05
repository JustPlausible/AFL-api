"""Scheduler-facing collection boundary with coordinated persistence."""

from collection import source_policy
from scheduler.write_lane import write_lane

def collect_scheduled(*args, **kwargs):
    """Collect/parse normally and route only persistence callbacks through the lane."""
    kwargs["write_executor"] = write_lane.execute
    return source_policy.collect_operational(*args, **kwargs)
