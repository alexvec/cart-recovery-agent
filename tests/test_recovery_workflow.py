"""Workflow tests on Temporal's time-skipping server: 72-hour flows run in milliseconds."""

import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from recovery.workflows.recovery import RecoveryWorkflow
from recovery.workflows.types import CallOutcome, EventIn, OrderInfo, PolicyResult, RecoveryInput

QUEUE = "test"


class Fakes:
    def __init__(self, decisions: list[PolicyResult]) -> None:
        self.decisions = decisions
        self.calls = 0
        self.events: list[str] = []

    def activities(self) -> list[object]:
        @activity.defn(name="evaluate_policy")
        async def evaluate_policy(inp: RecoveryInput) -> PolicyResult:
            return self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]

        @activity.defn(name="place_call")
        async def place_call(inp: RecoveryInput) -> str:
            self.calls += 1
            return f"conv-{self.calls}"

        @activity.defn(name="record_event")
        async def record_event(e: EventIn) -> None:
            self.events.append(e.type)

        return [evaluate_policy, place_call, record_event]


ALLOW = PolicyResult(True, "allowed")


@pytest.fixture
async def env():  # type: ignore[no-untyped-def]
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def start(
    env: WorkflowEnvironment, fakes: Fakes
) -> tuple[Worker, WorkflowHandle[RecoveryWorkflow, str]]:
    worker = Worker(env.client, task_queue=QUEUE, workflows=[RecoveryWorkflow], activities=fakes.activities())
    handle = await env.client.start_workflow(
        RecoveryWorkflow.run,
        RecoveryInput("s.myshopify.com", "tok"),
        id=f"wf-{uuid.uuid4()}",
        task_queue=QUEUE,
    )
    return worker, handle


async def test_order_before_call_cancels_the_call(env: WorkflowEnvironment) -> None:
    fakes = Fakes([ALLOW])
    worker, handle = await start(env, fakes)
    async with worker:
        await handle.signal(RecoveryWorkflow.order_created, OrderInfo(1, "550.00"))
        assert await handle.result() == "recovered"
    assert fakes.calls == 0


async def test_answered_call_then_order_is_attributed(env: WorkflowEnvironment) -> None:
    fakes = Fakes([ALLOW])
    worker, handle = await start(env, fakes)
    async with worker:
        await env.sleep(timedelta(minutes=21))
        await handle.signal(RecoveryWorkflow.call_finished, CallOutcome("completed", "conv-1", 80))
        await env.sleep(timedelta(hours=2))
        await handle.signal(RecoveryWorkflow.order_created, OrderInfo(7, "550.00"))
        assert await handle.result() == "recovered"
    assert fakes.calls == 1
    assert fakes.events == ["order_attributed"]


async def test_no_answer_retries_once_then_gives_up(env: WorkflowEnvironment) -> None:
    fakes = Fakes([ALLOW])
    worker, handle = await start(env, fakes)
    async with worker:
        await env.sleep(timedelta(minutes=21))
        await handle.signal(RecoveryWorkflow.call_finished, CallOutcome("no_answer"))
        await env.sleep(timedelta(hours=24, minutes=1))
        await handle.signal(RecoveryWorkflow.call_finished, CallOutcome("no_answer"))
        assert await handle.result() == "not_recovered"
    assert fakes.calls == 2


async def test_holdout_is_never_called_but_still_tracked(env: WorkflowEnvironment) -> None:
    fakes = Fakes([PolicyResult(False, "holdout")])
    worker, handle = await start(env, fakes)
    async with worker:
        await env.sleep(timedelta(hours=5))
        await handle.signal(RecoveryWorkflow.order_created, OrderInfo(9, "550.00"))
        assert await handle.result() == "recovered"
    assert fakes.calls == 0


async def test_checkout_updates_restart_the_quiet_period(env: WorkflowEnvironment) -> None:
    fakes = Fakes([ALLOW])
    worker, handle = await start(env, fakes)
    async with worker:
        await env.sleep(timedelta(minutes=15))
        await handle.signal(RecoveryWorkflow.checkout_updated)
        await env.sleep(timedelta(minutes=15))
        assert fakes.calls == 0  # 30 minutes in, but only 15 since the last update
        await env.sleep(timedelta(minutes=10))
        assert fakes.calls == 1
        await handle.signal(RecoveryWorkflow.order_created, OrderInfo(3, "1.00"))
        await handle.signal(RecoveryWorkflow.call_finished, CallOutcome("completed"))
        assert await handle.result() == "recovered"
