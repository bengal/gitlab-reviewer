"""Background execution (M6a): interval queue pump + nightly promotion.

- ``init_scheduler(app)`` wires the APScheduler jobs (see scheduler.py);
- ``worker`` is the pump: ``worker.pump_once()`` claims jobs and runs them
  on daemon threads, ``worker.drain()`` joins them (tests).
"""

from app.scheduler import worker
from app.scheduler.scheduler import init_scheduler

__all__ = ["init_scheduler", "worker"]
