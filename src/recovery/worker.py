import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from recovery.config import get_settings
from recovery.log_config import configure_logging
from recovery.workflows import activities
from recovery.workflows.recovery import RecoveryWorkflow


async def main() -> None:
    configure_logging()
    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[RecoveryWorkflow],
        activities=[activities.evaluate_policy, activities.place_call, activities.record_event],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
