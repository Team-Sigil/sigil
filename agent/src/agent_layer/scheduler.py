"""Scheduled triggers. The scheduler can only wake the agent: every money-moving step
still goes through the maker's guardrails, the Auditor and the engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from agent_layer.flows.scheduled import (
    learning_proposals,
    pace_check,
    reservoir_check,
    week_key,
    weekly_summary,
)
from agent_layer.llm import LLM
from agent_layer.maker.agent import MakerAgent
from agent_layer.platform_api import MakerAPI, PlatformAPIError

log = logging.getLogger(__name__)


class Jobs:
    """Each job iterates over dependents and isolates failures, so one bad record never
    stops the rest. All actions are idempotent, so re-running a job is safe."""

    def __init__(self, maker: MakerAgent, api: MakerAPI, llm: LLM, today: Callable[[], date] = date.today):
        self._maker = maker
        self._api = api
        self._llm = llm
        self._today = today

    def _each_dependent(self, fn: Callable[[str, str], None]) -> None:
        for dep in self._api.list_dependents():
            try:
                fn(dep.dependent_id, dep.guardian_id)
            except PlatformAPIError:
                log.exception("job failed for dependent %s", dep.dependent_id)

    def allowance_day(self) -> None:
        today = self._today()

        def run(dependent_id: str, _guardian_id: str) -> None:
            allowance = self._api.get_context(dependent_id).policy.allowance
            if allowance and allowance.weekday == today.weekday():
                self._maker.run_allowance(dependent_id, week_key(today))

        self._each_dependent(run)

    def pace(self) -> None:
        today = self._today()
        self._each_dependent(lambda dep, _g: pace_check(self._api, dep, today))

    def reservoir(self) -> None:
        reservoir_check(self._api, self._today())

    def weekly_summaries(self) -> None:
        today = self._today()
        self._each_dependent(lambda dep, _g: weekly_summary(self._llm, self._api, dep, today))

    def learning(self) -> None:
        for guardian_id in sorted({d.guardian_id for d in self._api.list_dependents()}):
            try:
                learning_proposals(self._api, guardian_id)
            except PlatformAPIError:
                log.exception("learning failed for guardian %s", guardian_id)

    def by_name(self) -> dict[str, Callable[[], None]]:
        return {
            "allowance": self.allowance_day,
            "pace": self.pace,
            "reservoir": self.reservoir,
            "weekly_summary": self.weekly_summaries,
            "learning": self.learning,
        }


SCHEDULE = {
    "allowance": CronTrigger(hour=7, minute=0),
    "pace": CronTrigger(hour=18, minute=0),
    "reservoir": CronTrigger(hour=9, minute=0),
    "weekly_summary": CronTrigger(day_of_week="sun", hour=19, minute=0),
    "learning": CronTrigger(day_of_week="sun", hour=19, minute=30),
}


def build_scheduler(jobs: Jobs, timezone: str = "Asia/Bangkok") -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=timezone)
    for name, fn in jobs.by_name().items():
        scheduler.add_job(fn, SCHEDULE[name], id=name, max_instances=1, coalesce=True, misfire_grace_time=3600)
    return scheduler
