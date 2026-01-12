import asyncio
import json
import sys
from pathlib import Path
from typing import Callable

import bittensor as bt
import click
from loguru import logger
from targon_client import TargonClient, ContainerDeployConfig
from targon_utils import ensure_running_container
from generator import Generator
from renderer import Renderer


_GENERATOR_POD_NAME: str = "generator"
_GENERATOR_PORT: int = 10006
_GENERATOR_HEALTH_CHECK_PATH: str = "/health"
_RENDER_POD_NAME: str = "render"
_RENDER_PORT: int = 8000
_RENDER_HEALTH_CHECK_PATH: str = "/health"
_RENDER_IMAGE_URL: str = "ghcr.io/404-repo/render-service:latest"
_JUDGE_POD_NAME: str = "judge"


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


@cli.command("start-generator")
@click.option("--image-url", required=True, help="URL of the generator image to start")
@click.option("--targon-api-key", required=True, help="Targon API key")
def start_generator_cmd(image_url: str, targon_api_key: str) -> None:
    """Start the generator container."""
    click.echo(f"Starting generator: {image_url}", err=True)
    
    try:
        asyncio.run(
            _create_container(
                image_url=image_url,
                container_name=_GENERATOR_POD_NAME,
                targon_api_key=targon_api_key,
                resource_name="h200-small",
                port=_GENERATOR_PORT,
                health_check_path=_GENERATOR_HEALTH_CHECK_PATH,
                echo=lambda msg: click.echo(msg, err=True),
            )
        )
        click.echo(json.dumps({"success": True, "image_url": image_url}))
    except KeyboardInterrupt:
        logger.warning("Generator start interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Generator start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("start-renderer")
@click.option("--targon-api-key", required=True, help="Targon API key")
def start_renderer_cmd(targon_api_key: str) -> None:
    """Start the renderer container."""
    click.echo(f"Starting renderer: {_RENDER_IMAGE_URL}", err=True)
    
    try:
        asyncio.run(
            _create_container(
                image_url=_RENDER_IMAGE_URL,
                container_name=_RENDER_POD_NAME,
                targon_api_key=targon_api_key,
                resource_name="rtx4090-small",
                port=_RENDER_PORT,
                health_check_path=_RENDER_HEALTH_CHECK_PATH,
                echo=lambda msg: click.echo(msg, err=True),
            )
        )
        click.echo(json.dumps({"success": True, "image_url": _RENDER_IMAGE_URL}))
    except KeyboardInterrupt:
        logger.warning("Renderer start interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Renderer start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("render")
@click.option("--data-dir", required=True, help="Path to the directory containing the .ply files to render")
@click.option("--endpoint", required=True, help="Renderer endpoint URL.")
@click.option("--output-dir", default="results", help="Path to the directory where the rendered images will be saved.")
def render_cmd(data_dir: str, endpoint: str, output_dir: str) -> None:
    """Render the .ply files using the renderer endpoint."""
    click.echo(f"Rendering {data_dir} with endpoint {endpoint}", err=True)
    try:
        renderer = Renderer(
            data_dir=data_dir,
            endpoint=endpoint,
            output_dir=output_dir,
        )
        asyncio.run(renderer.render())
        click.echo(json.dumps({"success": True, "output_dir": output_dir}))
    except KeyboardInterrupt:
        logger.warning("Renderer interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))


@cli.command("stop-pods")
@click.option("--targon-api-key", required=True, help="Targon API key.")
def stop_pods_cmd(targon_api_key: str) -> None:
    """Stop the generator, render and judge pods."""
    click.echo("Stopping pods...", err=True)
    async def _stop() -> None:
        async with TargonClient(api_key=targon_api_key) as targon:
            containers = await targon.list_containers(prefix=_GENERATOR_POD_NAME)
            for c in containers:
                click.echo(f"Stopping container {c.name} ({c.uid})", err=True)
                if c.name in [_GENERATOR_POD_NAME, _RENDER_POD_NAME, _JUDGE_POD_NAME]:
                    click.echo(f"Stopping container {c.name} ({c.uid})", err=True)
                    await targon.delete_container(c.uid)
    try:
        asyncio.run(_stop())
    except KeyboardInterrupt:
        logger.warning("Pods stop interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Pods stop failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("generate")
@click.option("--prompts-file", required=True, help="Path to the file with prompts that are valid URLs.")
@click.option("--endpoint", required=True, help="Generator endpoint URL.")
@click.option("--seed", required=True, help="Seed for generation.")
@click.option("--output-folder", default="results", help="Folder path where generated .ply files will be saved.")
def generate_cmd(
    prompts_file: str,
    endpoint: str,
    seed: str,
    output_folder: str,
) -> None:  
    """Generate models using the generator endpoint."""
    # Read prompts from prompt file
    click.echo("Reading prompts from file...", err=True)
    try:
        with open(prompts_file, "r") as f:
            prompts = [line.strip() for line in f.readlines() if line.strip()]
    except FileNotFoundError:
        click.echo(f"Prompts file {prompts_file} not found", err=True)
        raise SystemExit(1)
    except Exception as e:
        click.echo(f"Error reading prompts file: {e}", err=True)
        raise SystemExit(1)
    
    if not prompts:
        click.echo("No prompts found in file", err=True)
        raise SystemExit(1)
    
    click.echo(f"Found {len(prompts)} prompts to process", err=True)

    # Create Generator instance
    generator = Generator(
        endpoint=endpoint,
        seed=int(seed),
        output_folder=Path(output_folder),
        echo=lambda msg: click.echo(msg, err=True),
    )
    
    try:
        asyncio.run(generator.generate_all(prompts))
        click.echo(json.dumps({"success": True}))
    except KeyboardInterrupt:
        logger.warning("Generation interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        click.echo(f"Generation failed: {e}", err=True)
        raise SystemExit(1)


async def _create_container(
    image_url: str,
    container_name: str,
    targon_api_key: str,
    resource_name: str,
    port: int,
    health_check_path: str,
    echo: Callable[[str], None],
) -> None:
    """
    Create and deploy a container on Targon.

    Args:
        image_url: Docker image URL to deploy
        container_name: Name for the container
        targon_api_key: Targon API key for authentication
        resource_name: Targon resource name (e.g., "h200-small")
        port: Port number for the container
        health_check_path: Health check endpoint path (e.g., "/health") or full URL
        echo: Callback function for logging messages

    Raises:
        RuntimeError: If container deployment fails
        KeyboardInterrupt: If interrupted by user
    """
    from targon.client.serverless import ServerlessResourceListItem

    container: ServerlessResourceListItem | None = None
    try:
        echo("Connecting to Targon...")
        async with TargonClient(api_key=targon_api_key) as targon:
            config = ContainerDeployConfig(
                image=image_url,
                resource_name=resource_name,
                port=port,
                container_concurrency=1,
            )
            container = await ensure_running_container(
                client=targon,
                name=container_name,
                config=config,
                health_check_path=health_check_path,
                echo=echo,
            )
            if container:
                echo(f"Container deployed successfully. UID: {container.uid}")
                echo(f"Container URL: {container.url}")
            else:
                raise RuntimeError("Failed to deploy and start container")
    except (KeyboardInterrupt, asyncio.CancelledError):
        echo("\nInterrupted by user. Cleaning up...")
        if container:
            try:
                async with TargonClient(api_key=targon_api_key) as targon:
                    await targon.delete_container(container.uid)
                    echo("Container deleted successfully")
            except Exception as cleanup_error:
                echo(f"Error during cleanup: {cleanup_error}")
        raise


if __name__ == "__main__":
    cli()
