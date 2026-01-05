import asyncio
import json
import sys

import bittensor as bt
import click
from loguru import logger
from targon_client import TargonClient, ContainerDeployConfig
from targon_utils import ensure_running_container


@click.group()
@click.option(
    "-v", "--verbose", count=True, help="Verbosity: -v INFO, -vv DEBUG, -vvv TRACE"
)
def cli(verbose: int) -> None:
    levels = {0: "WARNING", 1: "INFO", 2: "DEBUG"}
    logger.remove()
    logger.add(sys.stderr, level=levels.get(verbose, "TRACE"))


@cli.command("commit-hash")
@click.option("--hash", "commit_hash", required=True, help="HF commit SHA")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option("--wallet.name", "wallet_name", required=True)
@click.option("--wallet.hotkey", "wallet_hotkey", required=True)
@click.option("--wallet.path", "wallet_path", default=None)
def commit_hash_cmd(
    commit_hash: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    """Commit HF revision hash on-chain."""
    _run_commit(
        data={"commit": commit_hash},
        netuid=netuid,
        subtensor_endpoint=subtensor_endpoint,
        wallet_name=wallet_name,
        wallet_hotkey=wallet_hotkey,
        wallet_path=wallet_path,
    )


@cli.command("commit-repo")
@click.option("--repo", required=True, help="HF repo id (e.g. user/repo)")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option("--wallet.name", "wallet_name", required=True)
@click.option("--wallet.hotkey", "wallet_hotkey", required=True)
@click.option("--wallet.path", "wallet_path", default=None)
def commit_repo_cmd(
    repo: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    """Commit HF repo on-chain."""
    _run_commit(
        data={"repo": repo},
        netuid=netuid,
        subtensor_endpoint=subtensor_endpoint,
        wallet_name=wallet_name,
        wallet_hotkey=wallet_hotkey,
        wallet_path=wallet_path,
    )


def _run_commit(
    *,
    data: dict,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
    logger.info(f"Committing {data} with wallet {wallet_name}@{wallet_hotkey}")

    async def _commit() -> None:
        async with bt.async_subtensor(subtensor_endpoint) as subtensor:
            payload = json.dumps(data)
            success, block = await subtensor.set_reveal_commitment(
                wallet=wallet,
                netuid=netuid,
                data=payload,
                blocks_until_reveal=2,
            )
            if success:
                logger.info(f"Committed at block {block}")
            else:
                raise RuntimeError(f"Commitment failed at block {block}")

    try:
        asyncio.run(_commit())
        click.echo(json.dumps({"success": True, **data}))
    except Exception as e:
        logger.error(f"Commit failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("list-all")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
def list_all_cmd(netuid: int, subtensor_endpoint: str) -> None:
    """List all revealed commitments."""

    async def _list() -> list[dict]:
        async with bt.async_subtensor(subtensor_endpoint) as subtensor:
            commitments = await subtensor.get_all_revealed_commitments(netuid=netuid)
            return _parse_commitments(commitments)

    results = asyncio.run(_list())
    for entry in results:
        click.echo(json.dumps(entry))


def _parse_commitments(commitments: dict) -> list[dict]:
    """Extract latest commit and repo for each hotkey, sorted by commit block."""
    results = []

    for hotkey, entries in commitments.items():
        latest_commit: tuple[int, str] | None = None
        latest_repo: tuple[int, str] | None = None

        for block, data in entries:
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue

            if commit_hash := parsed.get("commit"):
                if latest_commit is None or block > latest_commit[0]:
                    latest_commit = (block, commit_hash)

            if repo := parsed.get("repo"):
                if latest_repo is None or block > latest_repo[0]:
                    latest_repo = (block, repo)

        if latest_commit is None:
            continue

        results.append(
            {
                "hotkey": hotkey,
                "commit_hash": latest_commit[1],
                "commit_block": latest_commit[0],
                "repo": latest_repo[1] if latest_repo else None,
                "repo_block": latest_repo[0] if latest_repo else None,
            }
        )

    results.sort(key=lambda x: x["commit_block"])
    return results


@cli.command("check-image")
@click.option("--image-url", required=True, help="URL of the image to check")
@click.option("--targon-api-key", required=True, help="Targon API key")
def check_image_cmd(image_url: str, targon_api_key: str) -> None:
    """Check if the image is accessible."""
    logger.info(f"Checking image: {image_url}")
    click.echo(f"Checking image: {image_url}", err=True)
    
    async def _check() -> None:
        click.echo("Connecting to Targon...", err=True)
        async with TargonClient(api_key=targon_api_key) as targon:
            config = ContainerDeployConfig(
                image=image_url,
                resource_name="h200-small",
                port=10006,
                container_concurrency=1,
            )
            container = await ensure_running_container(
                client=targon,
                name="check-image",
                config=config,
                echo=lambda msg: click.echo(msg, err=True),
            )
            if container:
                click.echo(f"Container deployed successfully. UID: {container.uid}", err=True)
                click.echo("Cleaning up container...", err=True)
                await targon.delete_container(container.uid)
                click.echo("Container deleted successfully", err=True)
            else:
                raise RuntimeError("Failed to deploy and start container")
    
    try:
        asyncio.run(_check())
        logger.info("Image check completed successfully")
        click.echo(json.dumps({"success": True, "image_url": image_url}))
    except Exception as e:
        logger.error(f"Image check failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
