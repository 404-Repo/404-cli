import asyncio
import json
import sys
from pathlib import Path
from typing import Callable
import requests

import click
from loguru import logger
from targon_client import TargonClient, ContainerDeployConfig
from targon_utils import ensure_running_container
from generator import Generator
from renderer import Renderer
from targon.client.serverless import ServerlessResourceListItem
from judge import Judge
from models import State, Schedule


_GENERATOR_POD_NAME: str = "generator"
_GENERATOR_PORT: int = 10006
_GENERATOR_HEALTH_CHECK_PATH: str = "/health"
_RENDER_POD_NAME: str = "render"
_RENDER_PORT: int = 8000
_RENDER_HEALTH_CHECK_PATH: str = "/health"
_RENDER_IMAGE_URL: str = "ghcr.io/404-repo/render-service:latest"
_JUDGE_POD_NAME: str = "judge"
_JUDGE_PORT: int = 8000
_JUDGE_HEALTH_CHECK_PATH: str = "/health"
_JUDGE_IMAGE_URL: str = "vllm/vllm-openai:latest"
_JUDGE_ARGS: list[str] = [
    "--model", "zai-org/GLM-4.1V-9B-Thinking",
    "--max-model-len", "8096",
    "--tensor-parallel-size", "1",
    "--gpu-memory-utilization", "0.95",
    "--max-num-seqs", "4",
]
_JUDGE_MODEL: str = "zai-org/GLM-4.1V-9B-Thinking"
_GITHUB_URL: str = "https://raw.githubusercontent.com/404-Repo/404-active-competition/main"


@click.group()
@click.option(
    "-v", "--verbose", count=True, help="Verbosity: -v INFO, -vv DEBUG, -vvv TRACE"
)
def cli(verbose: int) -> None:
    levels = {0: "WARNING", 1: "INFO", 2: "DEBUG"}
    logger.remove()
    logger.add(sys.stderr, level=levels.get(verbose, "TRACE"))


def _fetch_state() -> State:
    """Download and parse state.json from GitHub."""
    state_url = f"{_GITHUB_URL}/state.json"
    try:
        response = requests.get(state_url, timeout=10)
        response.raise_for_status()
        # response.json() already returns a dict; use model_validate for dict input
        return State.model_validate(response.json())
    except requests.RequestException as e:
        logger.error(f"Failed to fetch state.json: {e}")
        raise RuntimeError(f"Failed to fetch state.json from {state_url}: {str(e)}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse state.json: {e}")
        raise RuntimeError(f"Failed to parse state.json: {str(e)}")


def _fetch_schedule(round_number: int) -> Schedule:
    """Download and parse schedule.json from GitHub for a specific round."""
    schedule_url = f"{_GITHUB_URL}/rounds/{round_number}/schedule.json"
    try:
        response = requests.get(schedule_url, timeout=10)
        response.raise_for_status()
        return Schedule.model_validate(response.json())
    except requests.RequestException as e:
        logger.error(f"Failed to fetch schedule.json for round {round_number}: {e}")
        raise RuntimeError(f"Failed to fetch schedule.json from {schedule_url}: {str(e)}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse schedule.json for round {round_number}: {e}")
        raise RuntimeError(f"Failed to parse schedule.json: {str(e)}")


async def _fetch_and_parse_commitments(
    subtensor_endpoint: str,
    netuid: int,
    round_number: int,
    schedule: Schedule,
    current_round: int,
) -> dict[str, dict]:
    """Fetch commitments from subtensor and parse them for a specific round."""
    import bittensor as bt # Bittensor import should be here because bittensor captures command line args for click otherwise
    async with bt.async_subtensor(subtensor_endpoint) as subtensor:
        raw_commitments = await subtensor.get_all_revealed_commitments(netuid=netuid)
        return _parse_commitments(raw_commitments, round_number, schedule, current_round)


@cli.command("commit-hash")
@click.option("--hash", "commit_hash", required=True, help="HF commit SHA")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option("--wallet.name", "wallet_name", required=True, help="Name of the bittensor wallet to use")
@click.option("--wallet.hotkey", "wallet_hotkey", required=True, help="Hotkey name of the wallet")
@click.option("--wallet.path", "wallet_path", default=None, help="Path to the wallet directory (default: ~/.bittensor)")
def commit_hash_cmd(
    commit_hash: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    """Commit revision hash on-chain."""
    import bittensor as bt # Bittensor import should be here because bittensor captures command line args for click otherwise
        
    try: 
        state = _fetch_state()
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1)

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1)

    try:
        current_block = asyncio.run(bt.async_subtensor(subtensor_endpoint).get_current_block())
        if current_block < schedule.earliest_reveal_block:
            click.echo(json.dumps({"success": False, "error": f"Current block {current_block} is before the earliest reveal block {schedule.earliest_reveal_block}"}))
            raise SystemExit(1)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch current block: {str(e)}"}))
        raise SystemExit(1)

    round_to_commit = state.current_round if current_block <= schedule.latest_reveal_block else state.current_round + 1
    try:
        commitments = asyncio.run(
            _fetch_and_parse_commitments(
                subtensor_endpoint=subtensor_endpoint,
                netuid=netuid,
                round_number=round_to_commit,
                schedule=schedule,
                current_round=state.current_round,
            )
        )
        wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        hotkey = wallet.hotkey.ss58_address
        if hotkey not in commitments:
            click.echo(f"WARNING: You have not commited repo and cdn_url for round {round_to_commit}.", err=True)
        elif not commitments[hotkey]["repo"] or not commitments[hotkey]["cdn_url"]:
            click.echo(f"WARNING: You have not commited repo and cdn_url for round {round_to_commit}.", err=True)
    except Exception as e:
        click.echo(f"WARNING: Failed to fetch information about your commitments in round {round_to_commit}: {str(e)}", err=True)
        
    _run_commit(
        data={"commit": commit_hash},
        netuid=netuid,
        subtensor_endpoint=subtensor_endpoint,
        wallet_name=wallet_name,
        wallet_hotkey=wallet_hotkey,
        wallet_path=wallet_path,
        state=state,
        current_round=round_to_commit,
    )


@cli.command("commit-repo-cdn")
@click.option("--repo", required=True, help="HF repo id (e.g. user/repo)")
@click.option(
    "--cdn-url", 
    required=True, 
    help="URL of the S3 compatible object storage that saves the generated PLY files"
)
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option("--wallet.name", "wallet_name", required=True, help="Name of the bittensor wallet to use")
@click.option("--wallet.hotkey", "wallet_hotkey", required=True, help="Hotkey name of the wallet")
@click.option("--wallet.path", "wallet_path", default=None, help="Path to the wallet directory (default: ~/.bittensor)")
def commit_repo_cdn_cmd(
    repo: str,
    cdn_url: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    """Commit repo and CDN URL on-chain."""
    import bittensor as bt # Bittensor import should be here because bittensor captures command line args for click otherwise
        
    try: 
        state = _fetch_state()
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1)

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1)

    try:
        current_block = asyncio.run(bt.async_subtensor(subtensor_endpoint).get_current_block())
        if current_block < schedule.earliest_reveal_block:
            click.echo(json.dumps({"success": False, "error": f"Current block {current_block} is before the earliest reveal block {schedule.earliest_reveal_block}"}))
            raise SystemExit(1)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch current block: {str(e)}"}))
        raise SystemExit(1)

    round_to_commit = state.current_round if current_block <= schedule.latest_reveal_block else state.current_round + 1
    try:
        commitments = asyncio.run(
            _fetch_and_parse_commitments(
                subtensor_endpoint=subtensor_endpoint,
                netuid=netuid,
                round_number=round_to_commit,
                schedule=schedule,
                current_round=state.current_round,
            )
        )
        wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        hotkey = wallet.hotkey.ss58_address
        if hotkey not in commitments:
            click.echo(json.dumps({"success": False, "error": f"You have not committed hash for round {round_to_commit}. Please commit hash first."}))
            raise SystemExit(1)
        elif not commitments[hotkey]["commit_hash"]:
            click.echo(json.dumps({"success": False, "error": f"You have not committed hash for round {round_to_commit}. Please commit hash first."}))
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch information about your commitments in round {round_to_commit}: {str(e)}"}))
        raise SystemExit(1)

    _run_commit(
        data={"repo": repo, "cdn_url": cdn_url},
        netuid=netuid,
        subtensor_endpoint=subtensor_endpoint,
        wallet_name=wallet_name,
        wallet_hotkey=wallet_hotkey,
        wallet_path=wallet_path,
        state=state,
        current_round=round_to_commit,
    )


def _run_commit(
    *,
    data: dict,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
    state: State,
    current_round: int,
) -> None:
    import bittensor as bt # Bittensor import should be here because bittensor captures --help command otherwise
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
                click.echo(f"Committed at block {block}")
            else:
                raise RuntimeError(f"Commitment failed at block {block}")

    try:
        asyncio.run(_commit())
        data["round"] = current_round
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
    # Ask user for round number interactively
    round_number: int = click.prompt("Enter round number", type=int)
    logger.info(f"Listing commitments for round {round_number}")

    # Case of the next round while current round is in progress should be handled here too.
    try:
        state = _fetch_state()
        current_round = state.current_round
        if round_number > current_round + 1:
            click.echo(json.dumps({"success": False, "error": f"Round {round_number} is not yet revealed. Next round is {current_round + 1}."}))
            raise SystemExit(1)
    except Exception as e:
        logger.error(f"Failed to fetch state: {e}")
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1)
    
    # Fetch schedule for the round.
    # If the round is the next round while current round is in progress, fetch the schedule for the current round.
    try:
        round_to_fetch = round_number if round_number <= current_round else current_round
        schedule = _fetch_schedule(round_to_fetch)
    except Exception as e:
        logger.error(f"Failed to fetch schedule: {e}")
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1)

    async def _list(round_number: int, schedule: Schedule, current_round: int) -> list[dict]:
        import bittensor as bt # Bittensor import should be here because bittensor captures command line args for click otherwise
        async with bt.async_subtensor(subtensor_endpoint) as subtensor:
            commitments = await subtensor.get_all_revealed_commitments(netuid=netuid)
            commitments_dict = _parse_commitments(commitments, round_number, schedule, current_round)
            results_list = list(commitments_dict.values())
            results_list.sort(key=lambda x: x["commit_block"])
            return results_list

    results = asyncio.run(_list(round_number, schedule, current_round))
    for entry in results:
        click.echo(json.dumps(entry))


def _parse_commitments(commitments: dict, round_number: int, schedule: Schedule, current_round: int) -> dict[str, dict]:
    """Extract latest commit and repo for each hotkey, sorted by commit block."""
    results: dict[str, dict] = {}

    for hotkey, entries in commitments.items():
        latest_commit: tuple[int, str] | None = None
        latest_repo: tuple[int, str] | None = None
        latest_cdn_url: tuple[int, str] | None = None

        for block, data in entries:
            if round_number == current_round + 1 and block <= schedule.latest_reveal_block:
                continue
            if round_number <= current_round and (block < schedule.earliest_reveal_block or block > schedule.latest_reveal_block):
                continue
            
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

            if cdn_url := parsed.get("cdn_url"):
                if latest_cdn_url is None or block > latest_cdn_url[0]:
                    latest_cdn_url = (block, cdn_url)

        if latest_commit is None:
            continue

        results[hotkey] = {
            "hotkey": hotkey,
            "commit_hash": latest_commit[1],
            "commit_block": latest_commit[0],
            "repo": latest_repo[1] if latest_repo else None,
            "repo_block": latest_repo[0] if latest_repo else None,
            "cdn_url": latest_cdn_url[1] if latest_cdn_url else None,
            "cdn_block": latest_cdn_url[0] if latest_cdn_url else None,
        }

    return results


@cli.command("start-generator")
@click.option("--image-url", required=True, help="URL of the generator image to start")
@click.option("--targon-api-key", required=True, help="Targon API key")
def start_generator_cmd(image_url: str, targon_api_key: str) -> None:
    """Start the generator container."""
    click.echo(f"Starting generator: {image_url}", err=True)
    
    try:
        container_url = asyncio.run(
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
        click.echo(json.dumps({"success": True, "container_url": container_url}))
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
        container_url = asyncio.run(
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
        click.echo(json.dumps({"success": True, "container_url": container_url}))
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


@cli.command("start-judge")
@click.option("--targon-api-key", required=True, help="Targon API key.")
def start_judge_cmd(targon_api_key: str) -> None:
    """Start the judge container."""
    click.echo(f"Starting judge: {_JUDGE_IMAGE_URL}", err=True)
    try:
        container_url = asyncio.run(
            _create_container(
                image_url=_JUDGE_IMAGE_URL,
                container_name=_JUDGE_POD_NAME,
                targon_api_key=targon_api_key,
                resource_name="rtx4090-small",
                port=_JUDGE_PORT,
                health_check_path=_JUDGE_HEALTH_CHECK_PATH,
                echo=lambda msg: click.echo(msg, err=True),
                args=_JUDGE_ARGS,
            )
        )
        click.echo(json.dumps({"success": True, "container_url": container_url}))
    except KeyboardInterrupt:
        logger.warning("Judge start interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Judge start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)

    
@cli.command("judge")
@click.option("--prompt-file", required=True, help="Path to the file with prompts that are valid URLs.")
@click.option("--image-dir-1", required=True, help="Path to the directory containing the first set of images.")
@click.option("--image-dir-2", required=True, help="Path to the directory containing the second set of images.")
@click.option("--endpoint", required=True, help="Judge endpoint URL.")
@click.option("--seed", required=True, help="Seed for generation.")
@click.option("--output-file", default="duels.json", help="Path to the JSON file where duel results will be saved (default: duels.json).")
def judge_cmd(
    prompt_file: str,
    image_dir_1: str,
    image_dir_2: str,
    endpoint: str,
    seed: str,
    output_file: str,
) -> None:
    """Judge the two sets of images using the judge endpoint."""
    click.echo(f"Judging {prompt_file} with endpoint {endpoint}", err=True)
    try:
        judge = Judge(
            model=_JUDGE_MODEL,
            endpoint=f"{endpoint}/v1",
            seed=int(seed),
            temperature=0.0,
            max_tokens=1024,
            timeout=30.0,
        )
        asyncio.run(judge.judge(Path(prompt_file), Path(image_dir_1), Path(image_dir_2), Path(output_file)))
        click.echo(json.dumps({"success": True, "output_file": output_file}))
    except KeyboardInterrupt:
        logger.warning("Judge interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Judge failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)
    finally:
        click.echo(json.dumps({"success": True}))

@cli.command("stop-pods")
@click.option("--targon-api-key", required=True, help="Targon API key.")
def stop_pods_cmd(targon_api_key: str) -> None:
    """Stop the generator, render and judge pods."""
    click.echo("Stopping pods...", err=True)
    async def _stop() -> None:
        async with TargonClient(api_key=targon_api_key) as targon:
            containers = await targon.list_containers()
            for c in containers:
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
    args: list[str] | None = None,
) -> str:
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

    container: ServerlessResourceListItem | None = None
    try:
        echo("Connecting to Targon...")
        async with TargonClient(api_key=targon_api_key) as targon:
            config = ContainerDeployConfig(
                image=image_url,
                resource_name=resource_name,
                port=port,
                container_concurrency=1,
                args=args,
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
                return container.url
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
