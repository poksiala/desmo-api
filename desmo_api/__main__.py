import argparse
import asyncio
import signal


from .jailer.jail_fsm import JailEventWorker
from .log import get_logger

logger = get_logger(__name__)


async def start_jailer():
    jailer = JailEventWorker(node_id="default")

    def handle_signal(signum: object, _frame: object):
        logger.info("Handling signal {}", signum)
        jailer.start_shutdown()

    signal.signal(signal.SIGINT, handle_signal)

    await jailer.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Desmofylakas")
    parser.add_argument("mode", choices=["api", "jailer"])
    args = parser.parse_args()
    if args.mode == "api":
        from .api import main

        asyncio.run(main())
    if args.mode == "jailer":
        asyncio.run(start_jailer())
