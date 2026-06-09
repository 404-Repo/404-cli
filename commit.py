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
from generator import Generator, PromptItem
from judge import Judge
from renderer import Renderer
from targon.client.serverless import ServerlessResourceListItem
from models import State, Schedule


_GENERATOR_POD_NAME: str = "generator"
_GENERATOR_PORT: int = 10006
_GENERATOR_HEALTH_CHECK_PATH: str = "/health"
_GENERATOR_RESOURCE_NAME: str = "h200-large"
_RENDER_POD_NAME: str = "render"
_RENDER_PORT: int = 8000
_RENDER_HEALTH_CHECK_PATH: str = "/health"
_RENDER_IMAGE_URL: str = (
    "europe-west3-docker.pkg.dev/gen-456515/active-competition/render-service-js:0.4.3"
)
_RENDER_RESOURCE_NAME: str = "cpu-small"
_JUDGE_POD_NAME: str = "judge"
_JUDGE_PORT: int = 8000
_JUDGE_HEALTH_CHECK_PATH: str = "/health"
_JUDGE_IMAGE_URL: str = "vllm/vllm-openai:v0.20.0"
_JUDGE_RESOURCE_NAME: str = "h200-small"
_JUDGE_ARGS: list[str] = [
    "zai-org/GLM-4.6V-Flash",
    "--revision",
    "411bb4d77144a3f03accbf4b780f5acb8b7cde4e",
    "--served-model-name",
    "glm-4.6v-flash",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--trust-remote-code",
    "--dtype",
    "bfloat16",
    "--gpu-memory-utilization",
    "0.90",
    "--max-model-len",
    "32768",
    "--max-num-seqs",
    "32",
]
_GITHUB_URL: str = (
    "https://raw.githubusercontent.com/404-Repo/404-active-competition/main"
)
_REQUIRED_LOCK_ALPHA: float = 100.0


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
        raise RuntimeError(
            f"Failed to fetch schedule.json from {schedule_url}: {str(e)}"
        )
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse schedule.json for round {round_number}: {e}")
        raise RuntimeError(f"Failed to parse schedule.json: {str(e)}")


def _decode_revealed_commitment(com: object, block: int) -> tuple[int, str]:
    """Decode a single revealed commitment, tolerant of both shapes the chain returns.

    Works around a bittensor 10.x bug: ``get_all_revealed_commitments`` assumes every
    commitment is a ``0x`` hex string and calls ``bytes.fromhex()``, but the runtime
    returns an already-decoded ``str`` whenever the payload is valid UTF-8 (raising
    "non-hexadecimal number found in fromhex()"). We handle both, then strip the SCALE
    compact-length prefix the same way bittensor does.
    """
    if isinstance(com, str) and com.startswith("0x"):
        raw = bytes.fromhex(com[2:])
    elif isinstance(com, str):
        raw = com.encode("utf-8", errors="ignore")
    else:
        raw = bytes(com)  # type: ignore[arg-type]
    if not raw:
        return block, ""
    mode = raw[0] & 0b11
    offset = 1 if mode == 0 else 2 if mode == 1 else 4
    return block, raw[offset:].decode("utf-8", errors="ignore")


async def _get_all_revealed_commitments(subtensor, netuid: int) -> dict[str, tuple]:
    """Robust replacement for ``subtensor.get_all_revealed_commitments`` (see above)."""
    query = await subtensor.query_map(
        module="Commitments", name="RevealedCommitments", params=[netuid]
    )
    result: dict[str, tuple] = {}
    async for hotkey, data in query:
        result[hotkey] = tuple(_decode_revealed_commitment(p[0], p[1]) for p in data)
    return result


async def _fetch_and_parse_commitments(
    subtensor_endpoint: str,
    netuid: int,
    round_number: int,
    schedule: Schedule,
    current_round: int,
) -> dict[str, dict]:
    """Fetch commitments from subtensor and parse them for a specific round."""
    import bittensor as bt  # Bittensor import should be here because bittensor captures command line args for click otherwise

    async with bt.AsyncSubtensor(subtensor_endpoint) as subtensor:
        raw_commitments = await _get_all_revealed_commitments(subtensor, netuid)
        return _parse_commitments(
            raw_commitments, round_number, schedule, current_round
        )


def _check_lock_status(
    subtensor_endpoint: str,
    coldkey_ss58: str,
    netuid: int,
    current_hotkey: str,
    commitment_hotkeys: set[str],
    per_hotkey_alpha: float,
) -> tuple[float, int, float]:
    """Return (locked_alpha, num_submitting_hotkeys, required_alpha) for a coldkey.

    The lock requirement scales with the number of this coldkey's hotkeys that have a
    submission this round (the hotkey being committed now always counts):
    required = per_hotkey_alpha * num_submitting_hotkeys.
    """
    import bittensor as bt  # Bittensor import should be here because bittensor captures command line args for click otherwise

    async def _query() -> "tuple[list[str], object | None]":
        async with bt.AsyncSubtensor(subtensor_endpoint) as subtensor:
            owned = await subtensor.get_owned_hotkeys(coldkey_ss58)
            lock = await subtensor.get_coldkey_lock(
                coldkey_ss58=coldkey_ss58, netuid=netuid
            )
            return owned, lock

    owned_hotkeys, lock = asyncio.run(_query())
    submitting = {hk for hk in commitment_hotkeys if hk in set(owned_hotkeys)}
    submitting.add(
        current_hotkey
    )  # the hotkey we are committing for belongs to this coldkey
    num_submitting = len(submitting)
    locked_alpha = float(lock["locked_mass"].tao) if lock is not None else 0.0
    return locked_alpha, num_submitting, num_submitting * per_hotkey_alpha


def _warn_if_insufficient_lock(
    subtensor_endpoint: str,
    coldkey_ss58: str,
    netuid: int,
    current_hotkey: str,
    commitment_hotkeys: set[str],
    per_hotkey_alpha: float,
) -> None:
    """Warn the miner (non-blocking) if their coldkey lacks the required conviction lock."""
    try:
        locked_alpha, num_submitting, required_alpha = _check_lock_status(
            subtensor_endpoint,
            coldkey_ss58,
            netuid,
            current_hotkey,
            commitment_hotkeys,
            per_hotkey_alpha,
        )
    except Exception as e:
        click.echo(
            f"WARNING: Failed to check conviction lock for your coldkey: {str(e)}",
            err=True,
        )
        return
    if locked_alpha < required_alpha:
        click.echo(
            f"WARNING: Your coldkey has only {locked_alpha:.4f} ρ locked on netuid {netuid}, but "
            f"{required_alpha:.0f} is required ({per_hotkey_alpha:.0f} ρ per submitting hotkey x {num_submitting}). "
            f"Lock at least {required_alpha:.0f} ρ (conviction) with your coldkey or your submissions may not be scored.",
            err=True,
        )


@cli.command("commit-hash")
@click.option("--hash", "commit_hash", required=True, help="HF commit SHA")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option(
    "--wallet.name",
    "wallet_name",
    required=True,
    help="Name of the bittensor wallet to use",
)
@click.option(
    "--wallet.hotkey", "wallet_hotkey", required=True, help="Hotkey name of the wallet"
)
@click.option(
    "--wallet.path",
    "wallet_path",
    default=None,
    help="Path to the wallet directory (default: ~/.bittensor)",
)
@click.option(
    "--required-lock",
    "required_lock_per_hotkey",
    type=float,
    default=_REQUIRED_LOCK_ALPHA,
    show_default=True,
    help="Required locked ρ (conviction) per submitting hotkey.",
)
def commit_hash_cmd(
    commit_hash: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
    required_lock_per_hotkey: float,
) -> None:
    """Commit revision hash on-chain."""
    import bittensor as bt  # Bittensor import should be here because bittensor captures command line args for click otherwise

    try:
        state = _fetch_state()
    except Exception as e:
        click.echo(
            json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"})
        )
        raise SystemExit(1)

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(
            json.dumps(
                {"success": False, "error": f"Failed to fetch schedule: {str(e)}"}
            )
        )
        raise SystemExit(1)

    try:
        current_block = asyncio.run(
            bt.AsyncSubtensor(subtensor_endpoint).get_current_block()
        )
        if current_block < schedule.earliest_reveal_block:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": f"Current block {current_block} is before the earliest reveal block {schedule.earliest_reveal_block}",
                    }
                )
            )
            raise SystemExit(1)
    except Exception as e:
        click.echo(
            json.dumps(
                {"success": False, "error": f"Failed to fetch current block: {str(e)}"}
            )
        )
        raise SystemExit(1)

    round_to_commit = (
        state.current_round
        if current_block <= schedule.latest_reveal_block
        else state.current_round + 1
    )
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
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        hotkey = wallet.hotkey.ss58_address
        if hotkey not in commitments:
            click.echo(
                f"WARNING: You have not commited repo and cdn_url for round {round_to_commit}.",
                err=True,
            )
        elif not commitments[hotkey]["repo"] or not commitments[hotkey]["cdn_url"]:
            click.echo(
                f"WARNING: You have not commited repo and cdn_url for round {round_to_commit}.",
                err=True,
            )
        _warn_if_insufficient_lock(
            subtensor_endpoint,
            wallet.coldkeypub.ss58_address,
            netuid,
            hotkey,
            set(commitments),
            required_lock_per_hotkey,
        )
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
@click.option("--repo", required=True, help="HF repo id (e.g. user/repo)")
@click.option(
    "--cdn-url",
    required=True,
    help="URL of the S3 compatible object storage that saves the generated PLY files",
)
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option(
    "--wallet.name",
    "wallet_name",
    required=True,
    help="Name of the bittensor wallet to use",
)
@click.option(
    "--wallet.hotkey", "wallet_hotkey", required=True, help="Hotkey name of the wallet"
)
@click.option(
    "--wallet.path",
    "wallet_path",
    default=None,
    help="Path to the wallet directory (default: ~/.bittensor)",
)
@click.option(
    "--required-lock",
    "required_lock_per_hotkey",
    type=float,
    default=_REQUIRED_LOCK_ALPHA,
    show_default=True,
    help="Required locked ρ (conviction) per submitting hotkey.",
)
def commit_repo_cdn_cmd(
    repo: str,
    cdn_url: str,
    netuid: int,
    subtensor_endpoint: str,
    wallet_name: str,
    wallet_hotkey: str,
    wallet_path: str | None,
    required_lock_per_hotkey: float,
) -> None:
    """Commit repo and CDN URL on-chain."""
    import bittensor as bt  # Bittensor import should be here because bittensor captures command line args for click otherwise

    try:
        state = _fetch_state()
    except Exception as e:
        click.echo(
            json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"})
        )
        raise SystemExit(1)

    try:
        schedule = _fetch_schedule(state.current_round)
    except Exception as e:
        click.echo(
            json.dumps(
                {"success": False, "error": f"Failed to fetch schedule: {str(e)}"}
            )
        )
        raise SystemExit(1)

    try:
        current_block = asyncio.run(
            bt.AsyncSubtensor(subtensor_endpoint).get_current_block()
        )
        if current_block < schedule.earliest_reveal_block:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": f"Current block {current_block} is before the earliest reveal block {schedule.earliest_reveal_block}",
                    }
                )
            )
            raise SystemExit(1)
    except Exception as e:
        click.echo(
            json.dumps(
                {"success": False, "error": f"Failed to fetch current block: {str(e)}"}
            )
        )
        raise SystemExit(1)

    round_to_commit = (
        state.current_round
        if current_block <= schedule.latest_reveal_block
        else state.current_round + 1
    )
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
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        hotkey = wallet.hotkey.ss58_address
        if hotkey not in commitments:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": f"You have not committed hash for round {round_to_commit}. Please commit hash first.",
                    }
                )
            )
            raise SystemExit(1)
        elif not commitments[hotkey]["commit_hash"]:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": f"You have not committed hash for round {round_to_commit}. Please commit hash first.",
                    }
                )
            )
            raise SystemExit(1)
        _warn_if_insufficient_lock(
            subtensor_endpoint,
            wallet.coldkeypub.ss58_address,
            netuid,
            hotkey,
            set(commitments),
            required_lock_per_hotkey,
        )
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
    import bittensor as bt  # Bittensor import should be here because bittensor captures --help command otherwise

    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
    logger.info(f"Committing {data} with wallet {wallet_name}@{wallet_hotkey}")

    async def _commit() -> None:
        async with bt.AsyncSubtensor(subtensor_endpoint) as subtensor:
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


def _render_commitments_table(results: list[dict], round_number: int) -> None:
    """Pretty-print revealed commitments as a colored rich table."""
    from rich.console import Console
    from rich.table import Table
    from rich import box

    def _short(addr: str | None, head: int = 6, tail: int = 4) -> str:
        if not addr:
            return "[dim]—[/]"
        return f"{addr[:head]}…{addr[-tail:]}"

    table = Table(
        title=f"Revealed commitments — round {round_number}",
        title_style="bold",
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
        expand=False,
    )
    table.add_column("Hotkey", no_wrap=True)
    table.add_column("Hash", style="dim", no_wrap=True)
    table.add_column("Repo", overflow="ellipsis", max_width=32)
    table.add_column("CDN", overflow="ellipsis", max_width=36)
    table.add_column("Coldkey", no_wrap=True)
    table.add_column("Locked ρ", justify="right", no_wrap=True)
    table.add_column("Req ρ", justify="right", no_wrap=True)
    table.add_column("OK", justify="center", no_wrap=True)

    n_ok = n_short = n_unknown = 0
    for e in results:
        sufficient = e.get("lock_sufficient")
        if sufficient is True:
            ok, locked_style = "[green]✓[/]", "green"
            n_ok += 1
        elif sufficient is False:
            ok, locked_style = "[red]✗[/]", "red"
            n_short += 1
        else:
            ok, locked_style = "[dim]?[/]", "dim"
            n_unknown += 1

        locked = e.get("locked_rho")
        required = e.get("required_rho")
        locked_str = (
            f"[{locked_style}]{locked:.2f}[/]" if locked is not None else "[dim]—[/]"
        )
        required_str = f"{required:.0f}" if required is not None else "[dim]—[/]"
        commit_hash = e.get("commit_hash") or ""

        table.add_row(
            _short(e.get("hotkey")),
            commit_hash[:10] if commit_hash else "[dim]—[/]",
            e.get("repo") or "[dim]—[/]",
            e.get("cdn_url") or "[dim]—[/]",
            _short(e.get("coldkey")),
            locked_str,
            required_str,
            ok,
        )

    console = Console()
    console.print(table)
    console.print(
        f"[bold]{len(results)}[/] commitments  "
        f"[green]{n_ok} sufficient[/]  "
        f"[red]{n_short} insufficient[/]  "
        f"[dim]{n_unknown} unknown[/]"
    )


@cli.command("list-all")
@click.option("--netuid", default=17, show_default=True)
@click.option(
    "--subtensor.endpoint", "subtensor_endpoint", default="finney", show_default=True
)
@click.option(
    "--required-lock",
    "required_lock_per_hotkey",
    type=float,
    default=_REQUIRED_LOCK_ALPHA,
    show_default=True,
    help="Required locked ρ (conviction) per submitting hotkey.",
)
@click.option(
    "--table",
    "as_table",
    is_flag=True,
    default=False,
    help="Render a colored table instead of JSON lines.",
)
def list_all_cmd(
    netuid: int,
    subtensor_endpoint: str,
    required_lock_per_hotkey: float,
    as_table: bool,
) -> None:
    """List all revealed commitments with each coldkey's locked stake and sufficiency."""
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
                        "error": f"Round {round_number} is not yet revealed. Next round is {current_round + 1}.",
                    }
                )
            )
            raise SystemExit(1)
    except Exception as e:
        logger.error(f"Failed to fetch state: {e}")
        click.echo(
            json.dumps({"success": False, "error": f"Failed to fetch state: {str(e)}"})
        )
        raise SystemExit(1)

    # Fetch schedule for the round.
    # If the round is the next round while current round is in progress, fetch the schedule for the current round.
    try:
        round_to_fetch = (
            round_number if round_number <= current_round else current_round
        )
        schedule = _fetch_schedule(round_to_fetch)
    except Exception as e:
        logger.error(f"Failed to fetch schedule: {e}")
        click.echo(
            json.dumps(
                {"success": False, "error": f"Failed to fetch schedule: {str(e)}"}
            )
        )
        raise SystemExit(1)

    async def _list(
        round_number: int, schedule: Schedule, current_round: int
    ) -> list[dict]:
        import bittensor as bt  # Bittensor import should be here because bittensor captures command line args for click otherwise
        from collections import Counter

        async with bt.AsyncSubtensor(subtensor_endpoint) as subtensor:
            commitments = await _get_all_revealed_commitments(subtensor, netuid)
            commitments_dict = _parse_commitments(
                commitments, round_number, schedule, current_round
            )

            # Resolve the coldkey owner of each submitting hotkey.
            hotkeys = list(commitments_dict)
            owner_results = await asyncio.gather(
                *(subtensor.get_hotkey_owner(hk) for hk in hotkeys),
                return_exceptions=True,
            )
            owners = {
                hk: (ck if isinstance(ck, str) else None)
                for hk, ck in zip(hotkeys, owner_results)
            }

            # The lock requirement scales with the number of a coldkey's submitting hotkeys.
            coldkey_counts = Counter(ck for ck in owners.values() if ck)
            unique_coldkeys = list(coldkey_counts)
            lock_results = await asyncio.gather(
                *(
                    subtensor.get_coldkey_lock(coldkey_ss58=ck, netuid=netuid)
                    for ck in unique_coldkeys
                ),
                return_exceptions=True,
            )
            locked_by_coldkey = {
                ck: (float(lock["locked_mass"].tao) if isinstance(lock, dict) else 0.0)
                for ck, lock in zip(unique_coldkeys, lock_results)
            }

            for entry in commitments_dict.values():
                ck = owners.get(entry["hotkey"])
                entry["coldkey"] = ck
                if ck is None:
                    # Could not resolve the owner (e.g. deregistered hotkey).
                    entry["locked_rho"] = None
                    entry["required_rho"] = None
                    entry["lock_sufficient"] = None
                else:
                    locked_rho = locked_by_coldkey.get(ck, 0.0)
                    required_rho = coldkey_counts[ck] * required_lock_per_hotkey
                    entry["locked_rho"] = locked_rho
                    entry["required_rho"] = required_rho
                    entry["lock_sufficient"] = locked_rho >= required_rho

            results_list = list(commitments_dict.values())
            results_list.sort(key=lambda x: x["commit_block"])
            return results_list

    results = asyncio.run(_list(round_number, schedule, current_round))
    if as_table:
        _render_commitments_table(results, round_number)
    else:
        for entry in results:
            click.echo(json.dumps(entry))


def _parse_commitments(
    commitments: dict, round_number: int, schedule: Schedule, current_round: int
) -> dict[str, dict]:
    """Extract latest commit and repo for each hotkey, sorted by commit block."""
    results: dict[str, dict] = {}

    for hotkey, entries in commitments.items():
        latest_commit: tuple[int, str] | None = None
        latest_repo: tuple[int, str] | None = None
        latest_cdn_url: tuple[int, str] | None = None

        for block, data in entries:
            if (
                round_number == current_round + 1
                and block <= schedule.latest_reveal_block
            ):
                continue
            if round_number <= current_round and (
                block < schedule.earliest_reveal_block
                or block > schedule.latest_reveal_block
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
def start_generator_cmd(image_url: str, targon_api_key: str) -> None:
    """Start the generator container."""
    click.echo(f"Starting generator: {image_url}", err=True)

    try:
        container_url = asyncio.run(
            _create_container(
                image_url=image_url,
                container_name=_GENERATOR_POD_NAME,
                targon_api_key=targon_api_key,
                resource_name=_GENERATOR_RESOURCE_NAME,
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
                resource_name=_RENDER_RESOURCE_NAME,
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
@click.option(
    "--data-dir",
    required=True,
    help="Path to the directory containing .js submission files to render",
)
@click.option("--endpoint", required=True, help="Renderer endpoint URL.")
@click.option(
    "--output-dir",
    default="results",
    help="Path to the directory where the rendered images will be saved.",
)
def render_cmd(data_dir: str, endpoint: str, output_dir: str) -> None:
    """Render .js submission files using the renderer endpoint."""
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
    """Start the judge VLLM container."""
    click.echo(f"Starting judge: {_JUDGE_IMAGE_URL}", err=True)

    try:
        container_url = asyncio.run(
            _create_container(
                image_url=_JUDGE_IMAGE_URL,
                container_name=_JUDGE_POD_NAME,
                targon_api_key=targon_api_key,
                resource_name=_JUDGE_RESOURCE_NAME,
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
        raise SystemExit(130)
    except Exception as e:
        logger.error(f"Judge start failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("judge")
@click.option(
    "--prompts-json",
    required=True,
    help="Path to prompts JSON with prompts[].stem and prompts[].image_url.",
)
@click.option(
    "--image-dir-1",
    required=True,
    help="Directory containing first rendered image set, grouped by stem.",
)
@click.option(
    "--image-dir-2",
    required=True,
    help="Directory containing second rendered image set, grouped by stem.",
)
@click.option(
    "--endpoint",
    required=True,
    help="Judge endpoint URL. /v1 is appended when omitted.",
)
@click.option("--seed", required=True, help="Seed for deterministic VLM calls.")
@click.option(
    "--output-dir",
    default="judge-results",
    show_default=True,
    help="Folder for per-duel JSON and duels.json.",
)
@click.option(
    "--concurrency",
    type=int,
    default=1,
    show_default=True,
    help="Maximum number of duels judged concurrently.",
)
def judge_cmd(
    prompts_json: str,
    image_dir_1: str,
    image_dir_2: str,
    endpoint: str,
    seed: str,
    output_dir: str,
    concurrency: int,
) -> None:
    """Judge two rendered image sets with the multi-stage duel pipeline."""
    click.echo(f"Judging {prompts_json} with endpoint {endpoint}", err=True)
    try:
        judge = Judge(
            endpoint=endpoint,
            seed=int(seed),
            output_dir=Path(output_dir),
            concurrency=concurrency,
            echo=lambda msg: click.echo(msg, err=True),
        )
        asyncio.run(
            judge.judge(Path(prompts_json), Path(image_dir_1), Path(image_dir_2))
        )
        click.echo(json.dumps({"success": True, "output_dir": output_dir}))
    except KeyboardInterrupt:
        logger.warning("Judge interrupted by user")
        click.echo(json.dumps({"success": False, "error": "Interrupted by user"}))
        raise SystemExit(130)
    except Exception as e:
        logger.error(f"Judge failed: {e}")
        click.echo(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)


@cli.command("stop-pods")
@click.option("--targon-api-key", required=True, help="Targon API key.")
def stop_pods_cmd(targon_api_key: str) -> None:
    """Stop the generator, render, and judge pods."""
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
@click.option(
    "--prompts-json",
    required=True,
    help='Path to JSON file: {"prompts":[{"stem","image_url"}],"seed":42}.',
)
@click.option("--endpoint", required=True, help="Generator endpoint URL.")
@click.option(
    "--seed",
    required=False,
    help="Optional seed override. If omitted, uses 'seed' from prompts JSON.",
)
@click.option(
    "--output-folder",
    default="results",
    help="Folder path where generated .js files will be saved.",
)
def generate_cmd(
    prompts_json: str,
    endpoint: str,
    seed: str | None,
    output_folder: str,
) -> None:
    """Generate models using the generator endpoint."""
    prompt_items: list[PromptItem] = []
    resolved_seed: int | None = int(seed) if seed is not None else None

    click.echo("Reading prompts from JSON file...", err=True)
    try:
        with open(prompts_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        click.echo(f"Prompts JSON file {prompts_json} not found", err=True)
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        click.echo(f"Invalid JSON in {prompts_json}: {e}", err=True)
        raise SystemExit(1)
    except Exception as e:
        click.echo(f"Error reading prompts JSON file: {e}", err=True)
        raise SystemExit(1)

    prompts_payload = payload.get("prompts")
    if not isinstance(prompts_payload, list) or not prompts_payload:
        click.echo("JSON must contain non-empty 'prompts' list", err=True)
        raise SystemExit(1)

    for idx, item in enumerate(prompts_payload):
        if not isinstance(item, dict):
            click.echo(f"prompts[{idx}] must be an object", err=True)
            raise SystemExit(1)
        image_url = item.get("image_url")
        stem = item.get("stem")
        if not isinstance(image_url, str) or not image_url.strip():
            click.echo(f"prompts[{idx}].image_url must be a non-empty string", err=True)
            raise SystemExit(1)
        if stem is not None and (not isinstance(stem, str) or not stem.strip()):
            click.echo(
                f"prompts[{idx}].stem must be a non-empty string when provided",
                err=True,
            )
            raise SystemExit(1)
        prompt_items.append(
            PromptItem(image_url=image_url.strip(), stem=stem.strip() if stem else None)
        )

    if resolved_seed is None:
        payload_seed = payload.get("seed")
        if payload_seed is None:
            click.echo(
                "Provide --seed or include numeric 'seed' in prompts JSON", err=True
            )
            raise SystemExit(1)
        try:
            resolved_seed = int(payload_seed)
        except (TypeError, ValueError):
            click.echo("'seed' in prompts JSON must be an integer", err=True)
            raise SystemExit(1)

    if resolved_seed is None:
        click.echo("Unable to resolve seed value", err=True)
        raise SystemExit(1)

    click.echo(f"Found {len(prompt_items)} prompts to process", err=True)

    # Create Generator instance
    generator = Generator(
        endpoint=endpoint,
        seed=resolved_seed,
        output_folder=Path(output_folder),
        echo=lambda msg: click.echo(msg, err=True),
    )

    try:
        asyncio.run(generator.generate_all(prompt_items))
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
