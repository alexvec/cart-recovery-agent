"""One durable workflow per abandoned checkout (docs/DESIGN.md section 3).

Waiting, calling hours, retries, "customer bought before we called" and attribution are
explicit steps here. Workflow code must be deterministic: all I/O happens in activities.
"""

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from recovery.workflows import activities
    from recovery.workflows.types import CallOutcome, EventIn, OrderInfo, PolicyResult, RecoveryInput

ACTIVITY_TIMEOUT = timedelta(seconds=30)
ACTIVITY_RETRY = RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0)
# Placing a call is not safely retryable: a retry after a timeout could ring the customer twice.
DIAL_RETRY = RetryPolicy(maximum_attempts=1)
CALL_RESULT_TIMEOUT = timedelta(minutes=30)
RETRY_AFTER_NO_ANSWER = timedelta(hours=24)


@workflow.defn
class RecoveryWorkflow:
    def __init__(self) -> None:
        self.order: OrderInfo | None = None
        self.checkout_touched = False
        self.call_outcome: CallOutcome | None = None
        self.state = "waiting"

    @workflow.signal
    def checkout_updated(self) -> None:
        self.checkout_touched = True

    @workflow.signal
    def order_created(self, order: OrderInfo) -> None:
        self.order = order

    @workflow.signal
    def call_finished(self, outcome: CallOutcome) -> None:
        self.call_outcome = outcome

    @workflow.query
    def status(self) -> str:
        return self.state

    async def _sleep_unless_ordered(self, duration: timedelta) -> None:
        try:
            await workflow.wait_condition(lambda: self.order is not None, timeout=duration)
        except TimeoutError:
            pass

    async def _event(self, inp: RecoveryInput, type_: str, data: dict[str, Any]) -> None:
        wid = workflow.info().workflow_id
        await workflow.execute_activity(
            activities.record_event,
            EventIn(wid, inp.shop, type_, data),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

    @workflow.run
    async def run(self, inp: RecoveryInput) -> str:
        started = workflow.now()

        # 1. Debounce: wait until the checkout has been quiet for the full delay.
        while self.order is None:
            self.checkout_touched = False
            try:
                await workflow.wait_condition(
                    lambda: self.order is not None or self.checkout_touched,
                    timeout=timedelta(minutes=inp.call_delay_minutes),
                )
            except TimeoutError:
                break

        # 2. Call attempts, each gated by the deterministic policy.
        attempts = 0
        deadline = started + timedelta(hours=inp.attribution_window_hours)
        while self.order is None and attempts < inp.max_attempts and workflow.now() < deadline:
            self.state = "policy"
            decision: PolicyResult = await workflow.execute_activity(
                activities.evaluate_policy,
                inp,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if not decision.allowed:
                if decision.reason == "outside_calling_hours" and decision.retry_at:
                    self.state = "waiting_for_calling_hours"
                    await self._sleep_unless_ordered(
                        datetime.fromisoformat(decision.retry_at) - workflow.now()
                    )
                    continue
                if decision.reason == "frequency_cap":
                    self.state = "waiting_for_frequency_cap"
                    await self._sleep_unless_ordered(RETRY_AFTER_NO_ANSWER)
                    continue
                break  # every other reason is final: no consent, holdout, do-not-call, ...

            self.state = "calling"
            self.call_outcome = None
            await workflow.execute_activity(
                activities.place_call, inp, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=DIAL_RETRY
            )
            attempts += 1
            try:
                await workflow.wait_condition(
                    lambda: self.call_outcome is not None, timeout=CALL_RESULT_TIMEOUT
                )
            except TimeoutError:
                self.call_outcome = CallOutcome("timeout")
            if self.call_outcome is not None and self.call_outcome.outcome == "completed":
                break
            if attempts < inp.max_attempts:
                self.state = "waiting_to_retry"
                await self._sleep_unless_ordered(RETRY_AFTER_NO_ANSWER)

        # 3. Attribution: keep listening for the order until the window closes, called or not.
        self.state = "attribution"
        remaining = deadline - workflow.now()
        if self.order is None and remaining > timedelta(0):
            await self._sleep_unless_ordered(remaining)

        if self.order is not None:
            await self._event(
                inp, "order_attributed", {"order_id": self.order.order_id, "value": self.order.value}
            )
            self.state = "recovered"
        else:
            await self._event(inp, "not_recovered", {"attempts": attempts})
            self.state = "not_recovered"
        return self.state
