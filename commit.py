import asyncio
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import click
import requests
from generator import Generator
from judge import Judge
from loguru import logger
from models import Schedule, State
from renderer import Renderer
from targon.client.serverless import ServerlessResourceListItem
from targon_client import ContainerDeployConfig, TargonClient
from targon_utils import ensure_running_container


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
    "--model",
    "zai-org/GLM-4.1V-9B-Thinking",
    "--max-model-len",
    "8096",
    "--tensor-parallel-size",
    "1",
    "--gpu-memory-utilization",
    "0.95",
    "--max-num-seqs",
    "4",
]
_JUDGE_MODEL: str = "zai-org/GLM-4.1V-9B-Thinking"
_GITHUB_URL: str = "https://raw.githubusercontent.com/404-Repo/404-active-competition/main"
_CLI_VERSION: str = "0.1.0"


@click.group()
@click.option("-v", "--verbose", count=True, help="Verbosity: -v INFO, -vv DEBUG, -vvv TRACE")
def cli(verbose: int) -> None:
    levels = {0: "WARNING", 1: "INFO", 2: "DEBUG"}
    logger.remove()
    logger.add(sys.stderr, level=levels.get(verbose, "TRACE"))


@cli.command("version")
def version_cmd() -> None:
    """Show the CLI version."""
    click.echo(_CLI_VERSION)


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
        raise RuntimeError(f"Failed to fetch state.json from {state_url}: {str(e)}") from e
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse state.json: {e}")
        raise RuntimeError(f"Failed to parse state.json: {str(e)}") from e


def _fetch_schedule(round_number: int) -> Schedule:
    """Download and parse schedule.json from GitHub for a specific round."""
    schedule_url = f"{_GITHUB_URL}/rounds/{round_number}/schedule.json"
    try:
        response = requests.get(schedule_url, timeout=10)
        response.raise_for_status()
        return Schedule.model_validate(response.json())
    except requests.RequestException as e:
        logger.error(f"Failed to fetch schedule.json for round {round_number}: {e}")
        raise RuntimeError(f"Failed to fetch schedule.json from {schedule_url}: {str(e)}") from e
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse schedule.json for round {round_number}: {e}")
        raise RuntimeError(f"Failed to parse schedule.json: {str(e)}") from e


def _validate_repo_format(repo: str) -> bool:
    """Validate that repo is in the format 'user/repo'."""
    # Single regex pattern with two groups: user and repo
    # Pattern: alphanumeric, hyphens, underscores, dots allowed
    pattern = r"([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)"
    return bool(re.fullmatch(pattern, repo))


def _validate_cdn_url_format(cdn_url: str) -> bool:
    """Validate that cdn_url is a valid URL format with http:// or https:// scheme."""
    try:
        result = urlparse(cdn_url)
        # Must have http or https scheme and a netloc (domain)
        return bool(result.scheme in ("http", "https") and result.netloc)
    except Exception:
        return False


async def _fetch_and_parse_commitments(
    subtensor_endpoint: str,
    netuid: int,
    round_number: int,
    schedule: Schedule,
    current_round: int,
) -> dict[str, dict]:
    """Fetch commitments from subtensor and parse them for a specific round."""
    # Bittensor import should be here because bittensor captures command line args for click otherwise
    import bittensor as bt

    async with bt.async_subtensor(subtensor_endpoint) as subtensor:
        raw_commitments = await subtensor.get_all_revealed_commitments(netuid=netuid)
        return _parse_commitments(raw_commitments, round_number, schedule, current_round)


@cli.command("commit-hash")
@click.option("--hash", "commit_hash", required=True, help="HF commit SHA")
@click.option("--netuid", default=17, show_default=True)
@click.option("--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True)
@click.option(
    "--wallet-name",
    "--name",
    "--wallet_name",
    "--wallet.name",
    "wallet_name",
    required=True,
    help="Name of the wallet.",
)
@click.option(
    "--wallet-path",
    "--wallet_path",
    "--wallet.path",
    "-p",
    "wallet_path",
    default=None,
    help=("Path where the wallets are located. " "For example: /Users/btuser/.bittensor/wallets."),
)
@click.option(
    "--hotkey",
    "--wallet_hotkey",
    "--wallet-hotkey",
    "--wallet.hotkey",
    "-H",
    "wallet_hotkey",
    required=True,
    help="Hotkey of the wallet",
)
def commit_hash_cmd(
    commit_hash: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
) -> None:
    """Commit revision hash on-chain."""
    # Bittensor import should be here because bittensor captures command line args for click otherwise
    import bittensor as bt

    try:
        state = _fetch_state()
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1) from None

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1) from None

    try:
        current_block = asyncio.run(bt.async_subtensor(subtensor_endpoint).get_current_block())
        if current_block < schedule.earliest_reveal_block:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Current block {current_block} is before the earliest reveal block "
                            f"{schedule.earliest_reveal_block}"
                        ),
                    }
                )
            )
            raise SystemExit(1) from None
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch current block: {str(e)}"}))
        raise SystemExit(1) from None

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
        click.echo(
            f"WARNING: Failed to fetch information about your commitments in round {round_to_commit}: {str(e)}",
            err=True,
        )

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
@click.option("--repo", required=True, help="Git repository in format 'user/repo' (e.g. mokabetrade/ansible-foundry)")
@click.option(
    "--cdn-url",
    required=True,
    help=(
        "URL of the S3 compatible object storage that saves the generated PLY files. "
        "Must be a valid URL with scheme (http:// or https://)"
    ),
)
@click.option("--netuid", default=17, show_default=True)
@click.option("--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True)
@click.option(
    "--wallet-name",
    "--name",
    "--wallet_name",
    "--wallet.name",
    "wallet_name",
    required=True,
    help="Name of the wallet.",
)
@click.option(
    "--wallet-path",
    "--wallet_path",
    "--wallet.path",
    "-p",
    "wallet_path",
    default=None,
    help=("Path where the wallets are located. " "For example: /Users/btuser/.bittensor/wallets."),
)
@click.option(
    "--hotkey",
    "--wallet_hotkey",
    "--wallet-hotkey",
    "--wallet.hotkey",
    "-H",
    "wallet_hotkey",
    required=True,
    help="Hotkey of the wallet",
)
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
    # Bittensor import should be here because bittensor captures command line args for click otherwise
    import bittensor as bt

    if not _validate_repo_format(repo):
        click.echo(
            json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Invalid git repository format: {repo}. "
                        "Expected format: 'user/repo' (e.g. 'mokabetrade/ansible-foundry')"
                    ),
                }
            )
        )
        raise SystemExit(1) from None

    # Validate CDN URL format
    if not _validate_cdn_url_format(cdn_url):
        click.echo(
            json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Invalid CDN URL format: {cdn_url}. " "Expected a valid URL with scheme (http:// or https://)"
                    ),
                }
            )
        )
        raise SystemExit(1) from None

    try:
        state = _fetch_state()
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1) from None

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1) from None

    try:
        current_block = asyncio.run(bt.async_subtensor(subtensor_endpoint).get_current_block())
        if current_block < schedule.earliest_reveal_block:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Current block {current_block} is before the earliest reveal block "
                            f"{schedule.earliest_reveal_block}"
                        ),
                    }
                )
            )
            raise SystemExit(1) from None
    except Exception as e:
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch current block: {str(e)}"}))
        raise SystemExit(1) from None

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
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"You have not committed hash for round {round_to_commit}. " "Please commit hash first."
                        ),
                    }
                )
            )
            raise SystemExit(1) from None
        elif not commitments[hotkey]["commit_hash"]:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"You have not committed hash for round {round_to_commit}. " "Please commit hash first."
                        ),
                    }
                )
            )
            raise SystemExit(1) from None
    except SystemExit:
        raise
    except Exception as e:
        click.echo(
            json.dumps(
                {
                    "success": False,
                    "error": f"Failed to fetch information about your commitments in round {round_to_commit}: {str(e)}",
                }
            )
        )
        raise SystemExit(1) from None

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
    # Bittensor import should be here because bittensor captures --help command otherwise
    import bittensor as bt

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
        raise SystemExit(1) from None


@cli.command("list-all")
@click.option("--netuid", default=17, show_default=True)
@click.option("--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True)
@click.option(
    "--exclude-partial",
    is_flag=True,
    default=False,
    help="Exclude partial commits (missing hash, repo, or cdn_url)",
)
def list_all_cmd(
    netuid: int,
    subtensor_endpoint: str,
    exclude_partial: bool,
) -> None:
    """List all revealed commitments."""
    # Ask user for round number interactively
    round_number: int = click.prompt("Enter round number", type=int)
    logger.info(f"Listing commitments for round {round_number}")

    # Case of the next round while current round is in progress should be handled here too.
    try:
        state = _fetch_state()
        current_round = state.current_round
        if round_number > current_round + 1:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": (f"Round {round_number} is not yet revealed. " f"Next round is {current_round + 1}."),
                    }
                )
            )
            raise SystemExit(1) from None
    except Exception as e:
        logger.error(f"Failed to fetch state: {e}")
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"}))
        raise SystemExit(1) from None

    # Fetch schedule for the round.
    # If the round is the next round while current round is in progress, fetch the schedule for the current round.
    try:
        round_to_fetch = round_number if round_number <= current_round else current_round
        schedule = _fetch_schedule(round_to_fetch)
    except Exception as e:
        logger.error(f"Failed to fetch schedule: {e}")
        click.echo(json.dumps({"success": False, "error": f"Failed to fetch schedule: {str(e)}"}))
        raise SystemExit(1) from None

    async def _list(round_number: int, schedule: Schedule, current_round: int, exclude_partial: bool) -> list[dict]:
        # Bittensor import should be here because bittensor captures command line args for click otherwise
        import bittensor as bt

        async with bt.async_subtensor(subtensor_endpoint) as subtensor:
            commitments = await subtensor.get_all_revealed_commitments(netuid=netuid)
            commitments_dict = _parse_commitments(commitments, round_number, schedule, current_round)
            results_list = list(commitments_dict.values())

            # Filter out partial commits if requested
            if exclude_partial:
                results_list = [
                    entry
                    for entry in results_list
                    if entry.get("commit_hash") and entry.get("repo") and entry.get("cdn_url")
                ]

            results_list.sort(key=lambda x: x["commit_block"])
            return results_list

    results = asyncio.run(_list(round_number, schedule, current_round, exclude_partial))

    if not results:
        click.echo("No commitments found for this round.", err=True)
        return

    # Always display as table
    _display_commitments_table(results, round_number)


def _display_commitments_table(results: list[dict], round_number: int) -> None:
    """Display commitments in a formatted table."""
    click.echo(f"\n{'='*140}", err=True)
    click.echo(f"Round {round_number} - Commitments ({len(results)} total)", err=True)
    click.echo(f"{'='*140}\n", err=True)

    # Compute dynamic widths so that repo and CDN URL are full-text but aligned
    repo_header = "Repo"
    cdn_header = "CDN URL"
    max_repo_len = max(len(entry.get("repo") or "N/A") for entry in results) if results else len(repo_header)
    max_cdn_len = max(len(entry.get("cdn_url") or "N/A") for entry in results) if results else len(cdn_header)
    repo_width = max(max_repo_len, len(repo_header))
    cdn_width = max(max_cdn_len, len(cdn_header))

    # Table header with dynamic spacing; hotkey shows first 8 chars
    header = (
        f"{'#':<4} "
        f"{'Hotkey':<10} "
        f"{'Block':<10} "
        f"{'Commit Hash':<40} "
        f"{repo_header:<{repo_width}} "
        f"{cdn_header:<{cdn_width}}"
    )
    click.echo(header, err=True)
    click.echo("-" * 140, err=True)

    # Table rows
    for idx, entry in enumerate(results, 1):
        full_hotkey = entry.get("hotkey", "N/A")
        hotkey = full_hotkey[:8] if full_hotkey != "N/A" else full_hotkey

        commit_hash = entry.get("commit_hash") or "N/A"
        commit_block = str(entry.get("commit_block") or "N/A")

        repo = entry.get("repo") or "N/A"
        cdn_url = entry.get("cdn_url") or "N/A"

        row = (
            f"{idx:<4} "
            f"{hotkey:<10} "
            f"{commit_block:<10} "
            f"{commit_hash:<40} "
            f"{repo:<{repo_width}} "
            f"{cdn_url:<{cdn_width}}"
        )
        click.echo(row, err=True)

    click.echo(f"\n{'='*140}\n", err=True)


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
            if round_number <= current_round and (
                block < schedule.earliest_reveal_block or block > schedule.latest_reveal_block
            ):
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
@click.option(
    "--hf-token",
    "hf_token",
    default=None,
    help="HuggingFace token to pass as HF_TOKEN environment variable",
)
@click.option("--name", "container_name", default=None, help="Custom container name (default: generator)")
@click.option(
    "--container-concurrency",
    "container_concurrency",
    type=int,
    default=1,
    show_default=True,
    help="Maximum concurrent requests per generator replica.",
)
@click.option(
    "--min-replicas",
    "min_replicas",
    type=int,
    default=1,
    show_default=True,
    help="Minimum number of generator replicas.",
)
@click.option(
    "--max-replicas",
    "max_replicas",
    type=int,
    default=2,
    show_default=True,
    help="Maximum number of generator replicas.",
)
def start_generator_cmd(
    image_url: str,
    targon_api_key: str,
    hf_token: str | None,
    container_name: str | None,
    container_concurrency: int,
    min_replicas: int,
    max_replicas: int,
) -> None:
    """Start the generator container."""
    click.echo(f"Starting generator: {image_url}", err=True)

    try:
        env = None
        if hf_token:
            env = {"HF_TOKEN": hf_token}

        # Format container name: "generator_{name}" if name provided, otherwise use default
        if container_name:
            name = f"generator_{container_name}"
        else:
            name = _GENERATOR_POD_NAME

        container_url = asyncio.run(
            _create_container(
                image_url=image_url,
                container_name=name,
                targon_api_key=targon_api_key,
                resource_name="h200-small",
                port=_GENERATOR_PORT,
                health_check_path=_GENERATOR_HEALTH_CHECK_PATH,
                echo=lambda msg: click.echo(msg, err=True),
                env=env,
                container_concurrency=container_concurrency,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
            )
        )
        click.echo(json.dumps({"success": True, "container_url": container_url}))
    except KeyboardInterrupt:
        logger.warning("Generator start interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Generator start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1) from None


@cli.command("start-renderer")
@click.option("--targon-api-key", required=True, help="Targon API key")
@click.option(
    "--container-concurrency",
    "container_concurrency",
    type=int,
    default=1,
    show_default=True,
    help="Maximum concurrent requests per renderer replica.",
)
@click.option(
    "--min-replicas",
    "min_replicas",
    type=int,
    default=1,
    show_default=True,
    help="Minimum number of renderer replicas.",
)
@click.option(
    "--max-replicas",
    "max_replicas",
    type=int,
    default=2,
    show_default=True,
    help="Maximum number of renderer replicas.",
)
def start_renderer_cmd(
    targon_api_key: str,
    container_concurrency: int,
    min_replicas: int,
    max_replicas: int,
) -> None:
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
                container_concurrency=container_concurrency,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
            )
        )
        click.echo(json.dumps({"success": True, "container_url": container_url}))
    except KeyboardInterrupt:
        logger.warning("Renderer start interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Renderer start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1) from None


@cli.command("render")
@click.option("--data-dir", required=True, help="Path to the directory containing the .ply files to render")
@click.option("--endpoint", required=True, help="Renderer endpoint URL.")
@click.option("--output-dir", default="results", help="Path to the directory where the rendered images will be saved.")
@click.option(
    "--concurrency",
    type=int,
    default=1,
    show_default=True,
    help="Maximum number of files rendered concurrently.",
)
def render_cmd(data_dir: str, endpoint: str, output_dir: str, concurrency: int) -> None:
    """Render the .ply files using the renderer endpoint."""
    click.echo(f"Rendering {data_dir} with endpoint {endpoint}", err=True)
    try:
        renderer = Renderer(
            data_dir=data_dir,
            endpoint=endpoint,
            output_dir=output_dir,
            concurrency=concurrency,
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
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Judge start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1) from None


@cli.command("judge")
@click.option("--prompt-file", required=True, help="Path to the file with prompts that are valid URLs.")
@click.option("--image-dir-1", required=True, help="Path to the directory containing the first set of images.")
@click.option("--image-dir-2", required=True, help="Path to the directory containing the second set of images.")
@click.option("--endpoint", required=True, help="Judge endpoint URL.")
@click.option("--seed", required=True, help="Seed for generation.")
@click.option(
    "--output-file",
    default="duels.json",
    help="Path to the JSON file where duel results will be saved (default: duels.json).",
)
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
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Judge failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1) from None
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
                # Stop all containers that start with "generator_", plus render and judge pods
                if c.name.startswith("generator_") or c.name in [
                    _RENDER_POD_NAME,
                    _JUDGE_POD_NAME,
                ]:
                    click.echo(f"Stopping container {c.name} ({c.uid})", err=True)
                    await targon.delete_container(c.uid)

    try:
        asyncio.run(_stop())
    except KeyboardInterrupt:
        logger.warning("Pods stop interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Pods stop failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1) from None


@cli.command("generate")
@click.option("--prompts-file", required=True, help="Path to the file with prompts that are valid URLs.")
@click.option("--endpoint", required=True, help="Generator endpoint URL.")
@click.option("--seed", required=True, help="Seed for generation.")
@click.option("--output-folder", default="results", help="Folder path where generated .ply files will be saved.")
@click.option(
    "--concurrency",
    type=int,
    default=8,
    show_default=True,
    help="Maximum number of prompts / HTTP requests processed concurrently.",
)
def generate_cmd(
    prompts_file: str,
    endpoint: str,
    seed: str,
    output_folder: str,
    concurrency: int,
) -> None:
    """Generate models using the generator endpoint."""
    # Read prompts from prompt file
    click.echo("Reading prompts from file...", err=True)
    try:
        with Path(prompts_file).open() as f:
            prompts = [line.strip() for line in f.readlines() if line.strip()]
    except FileNotFoundError:
        click.echo(f"Prompts file {prompts_file} not found", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Error reading prompts file: {e}", err=True)
        raise SystemExit(1) from None

    if not prompts:
        click.echo("No prompts found in file", err=True)
        raise SystemExit(1) from None

    click.echo(f"Found {len(prompts)} prompts to process", err=True)

    # Create Generator instance
    generator = Generator(
        endpoint=endpoint,
        seed=int(seed),
        output_folder=Path(output_folder),
        echo=lambda msg: click.echo(msg, err=True),
        concurrency=concurrency,
    )

    try:
        asyncio.run(generator.generate_all(prompts))
        click.echo(json.dumps({"success": True}))
    except KeyboardInterrupt:
        logger.warning("Generation interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130) from None  # Standard exit code for SIGINT
    except Exception as e:
        click.echo(f"Generation failed: {e}", err=True)
        raise SystemExit(1) from None


async def _create_container(
    image_url: str,
    container_name: str,
    targon_api_key: str,
    resource_name: str,
    port: int,
    health_check_path: str,
    echo: Callable[[str], None],
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    container_concurrency: int = 1,
    min_replicas: int = 1,
    max_replicas: int = 2,
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
                container_concurrency=container_concurrency,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                args=args,
                env=env,
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
                url: str = str(container.url)
                return url
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
