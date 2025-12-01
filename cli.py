import json
import asyncio
from loguru import logger
import click
import bittensor as bt
import sys
from settings import settings
from subtensor_wrapper import get_subtensor


class MetadataError(Exception):
    """Exception raised when metadata is not found."""
    pass



@click.group()
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (-v INFO, -vv DEBUG, -vvv TRACE)",
)
def cli(verbose):
    if verbose >= 3:
        level = "TRACE"
    elif verbose == 2:
        level = "DEBUG"
    elif verbose == 1:
        level = "INFO"
    else:
        level = "CRITICAL"

    logger.remove()
    logger.add(sys.stderr, level=level)


@cli.command("commit-hash")
@click.option("--hash", required=True, help="HF commit SHA")
@click.option("--coldkey", default=None, help="Name of the cold wallet to use.")
@click.option("--hotkey", default=None, help="Name of the hot wallet to use.")
def commit_hash(hash: str, coldkey: str, hotkey: str):
    """Commit repo+revision on-chain (separate from deployment)."""
    wallet_name = coldkey or settings.wallet_name
    wallet_hotkey = hotkey or settings.wallet_hotkey
    wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey)

    logger.info(f"Committing {hash} with wallet {wallet_name}@{wallet_hotkey}")

    async def _commit_hash():
        sub = await get_subtensor()
        data = json.dumps({"commit_hash": hash})
        while True:
            try:
                await sub.set_reveal_commitment(
                    wallet=wallet, netuid=settings.netuid, data=data, blocks_until_reveal=2
                )
                break
            except MetadataError as e:
                if "SpaceLimitExceeded" in str(e):
                    await sub.wait_for_block()
                else:
                    raise

    try:
        asyncio.run(_commit_hash())
        click.echo(
            json.dumps(
                {
                    "success": True,
                    "commit_hash": hash,
                }
            )
        )
    except Exception as e:
        logger.error("Commit failed: %s", e)
        click.echo(json.dumps({"success": False, "error": str(e)}))


@cli.command("commit-repo")
@click.option("--repo", required=True, help="HF repo id (e.g. <user>/<repo>)")
@click.option("--coldkey", default=None, help="Name of the cold wallet to use.")
@click.option("--hotkey", default=None, help="Name of the hot wallet to use.")
def commit_repo(repo: str, coldkey: str, hotkey: str):
    """Commit repo+revision on-chain (separate from deployment)."""
    wallet_name = coldkey or settings.wallet_name
    wallet_hotkey = hotkey or settings.wallet_hotkey
    wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey)

    logger.info(f"Committing {repo} with wallet {wallet_name}@{wallet_hotkey}")

    async def _commit_repo():
        sub = await get_subtensor()
        data = json.dumps({"repo": repo})
        while True:
            try:
                await sub.set_reveal_commitment(
                    wallet=wallet, netuid=settings.netuid, data=data, blocks_until_reveal=1
                )
                break
            except MetadataError as e:
                if "SpaceLimitExceeded" in str(e):
                    await sub.wait_for_block()
                else:
                    raise

    try:
        asyncio.run(_commit_repo())
        click.echo(
            json.dumps(
                {
                    "success": True,
                    "repo": repo,
                }
            )
        )
    except Exception as e:
        logger.error("Commit failed: %s", e)
        click.echo(json.dumps({"success": False, "error": str(e)}))
