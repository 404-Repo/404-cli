from loguru import logger
from settings import settings
from subtensor_wrapper import get_subtensor
from tests.submission import parse_commitment
import asyncio


async def check_submissions() -> None:
    sub = await get_subtensor()
    commitments = await sub.get_all_revealed_commitments(netuid=settings.netuid)
    logger.debug(f"Revealed commitments: {commitments}")

    submissions = []
    for hotkey, commitment in commitments.items():
        submission = parse_commitment(
            hotkey=hotkey,
            commitment=commitment,
            earliest_block=0,
            latest_block=1000000000000000000000000000000000000000,
        )
        if submission:
            submissions.append(submission)

    submissions.sort(key=lambda x: x.reveal_block)
    logger.success(f"Found {len(submissions)} valid submissions")

    return submissions


async def main() -> None:
    while True:
        try:
            submissions = await check_submissions()
            if submissions:
                logger.success(f"Found {len(submissions)} valid submissions")
                break
        except Exception as e:
            logger.error(f"Error checking submissions: {e}")
        finally:
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
