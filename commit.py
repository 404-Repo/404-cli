import asyncio
import json
import sys

import bittensor as bt
import httpx
import click
from loguru import logger
from targon_client import TargonClient, ContainerDeployConfig
from targon_utils import ensure_running_container
from r2_client import R2Client
from typing import Callable


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


@cli.command("generate")
@click.option("--prompts-file", required=True, help="Path to the file with prompts that are valid URLs.")
@click.option("--image-url", required=True, help="URL of docker image to use for generation.")
@click.option("--targon-api-key", required=True, help="Targon API key.")
@click.option("--s3-access-key-id", required=True, help="S3 access key ID.")
@click.option("--s3-secret-access-key", required=True, help="S3 secret access key.")
@click.option("--s3-bucket-name", required=True, help="S3 bucket name where to save generated models.")
@click.option("--s3-url", required=True, help="S3 URL where to save generated models.")
@click.option("--seed", required=True, help="Seed for generation.")
@click.option("--folder", default="results", help="Folder to save generated models.")
def generate_cmd(
    prompts_file: str,
    image_url: str,
    targon_api_key: str,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    s3_bucket_name: str,
    s3_url: str,
    seed: str,
    folder: str,
) -> None:  
    """Generate models using Targon."""
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

    async def _generate(prompts: list[str]) -> None:
        container = None
        try:
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
                        name="ply-generator",
                        config=config,
                        echo=lambda msg: click.echo(msg, err=True),
                    )
                    if not container:
                        click.echo("Failed to deploy and start container", err=True)
                        raise SystemExit(1)
                    
                    async with R2Client(
                        bucket=s3_bucket_name,
                        access_key_id=s3_access_key_id,
                        secret_access_key=s3_secret_access_key,
                        r2_endpoint=s3_url,
                    ) as r2:
                        click.echo(f"Processing {len(prompts)} prompts...", err=True)
                        request_sem = asyncio.Semaphore(1)  # Using semaphores to limit request to one at a time.
                        process_sem = asyncio.Semaphore(8)  # Limiting request to control traffic
                        tasks = [
                            _process_prompt(
                                request_sem=request_sem,
                                process_sem=process_sem,
                                endpoint=container.url,
                                prompt=prompt,
                                r2_client=r2,
                                folder=folder,
                                echo=lambda msg: click.echo(msg, err=True),
                                seed=seed,
                            )
                            for prompt in prompts
                        ]
                        click.echo(f"Generated {len(tasks)} tasks", err=True)
                        results = await asyncio.gather(*tasks, return_exceptions=True)    
                        for prompt, result in zip(prompts, results):
                            if isinstance(result, Exception):
                                click.echo(f"Prompt {prompt} generation failed: {result}", err=True)
                            else:
                                click.echo(f"Prompt {prompt} generation successful", err=True)
            click.echo("Generation completed", err=True)
        except Exception as e:
            click.echo(f"Generation failed: {e}", err=True)
            raise SystemExit(1)
        finally:
            if container:
                async with TargonClient(api_key=targon_api_key) as targon:
                    click.echo("Deleting container...", err=True)
                    await targon.delete_container(container.uid)
    
    try:
        asyncio.run(_generate(prompts=prompts))
        click.echo(json.dumps({"success": True}))
    except Exception as e:
        click.echo(f"Generation failed: {e}", err=True)
        raise SystemExit(1)


async def _process_prompt(
    *,
    request_sem: asyncio.Semaphore,
    process_sem: asyncio.Semaphore,
    endpoint: str,
    prompt: str,
    r2_client: R2Client,
    folder: str,
    echo: Callable[[str], None],
    seed: int,
) -> None:
    """
    Downloads a prompt image from a public URL, generates a 3D model from it, and uploads the result to R2.
    """
    prompt_key = prompt.split("/")[-1].split(".")[0]

    async with process_sem:
        # Download image from public URL
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
        echo(f"Downloading image from {prompt}...")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(prompt)
            response.raise_for_status()
            image = response.content
            result = await _generate(
                request_sem=request_sem,
                endpoint=endpoint,
                image=image,
                echo=echo,
                prompt_key=prompt_key,
                seed=seed,
            )
            if result is not None:
                echo(f"Prompt {prompt_key} generation successful")
            else:
                echo(f"Prompt {prompt_key} generation failed")
                raise RuntimeError(f"Prompt {prompt_key} generation failed")

        await r2_client.upload(
            key=f"{folder}/{prompt_key}.ply",
            data=result,
        )


async def _generate(
    *,
    request_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,   
    echo: Callable[[str], None],
    prompt_key: str,
    seed: int,
) -> bytes | None:
    """
    Generates a 3D model from an image using the Targon container with retries.
    """
    max_attempts = 3
    generation_http_backoff_base = 1.5
    generation_http_backoff_max = 10.0

    for attempt in range(max_attempts):
        if attempt > 0:
            backoff = min(
                generation_http_backoff_base * (2 ** (attempt - 1)),
                generation_http_backoff_max,
            )
            echo(f"Prompt {prompt_key} generation attempt {attempt + 1}/{max_attempts} after {backoff:.1f}s")
            await asyncio.sleep(backoff)

        result = await _generate_attempt(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            echo=echo,
            prompt_key=prompt_key,
            seed=seed,
        )

        if result is not None:  # None means retryable failure — continue to next attempt
            return result

    raise RuntimeError(f"Prompt {prompt_key} generation failed")


async def _generate_attempt(
    *,
    request_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,
    echo: Callable[[str], None],
    prompt_key: str,
    seed: int,
) -> bytes | None:
    """Single generation attempt."""
    sem_released = False
    await request_sem.acquire()

    try:
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                start_time = asyncio.get_running_loop().time()
                async with client.stream(
                    "POST",
                    f"{endpoint}/generate",
                    files={"prompt_image_file": ("prompt.jpg", image, "image/jpeg")},
                    data={"seed": seed},
                ) as response:
                    response.raise_for_status()

                    elapsed = asyncio.get_running_loop().time() - start_time

                    request_sem.release()
                    sem_released = True

                    echo(f"Prompt {prompt_key} generation completed in {elapsed:.1f}s")

                    try:
                        content = await response.aread()
                    except Exception as e:
                        return None

                    download_time = asyncio.get_running_loop().time() - start_time - elapsed
                    mb_size = len(content) / 1024 / 1024
                    echo(
                        f"Prompt {prompt_key} generated in {elapsed:.1f}s, "
                        f"downloaded in {download_time:.1f}s, {mb_size:.1f} MiB"
                    )

                    return content

            except Exception as e:
                echo(f"Prompt {prompt_key} generation failed: {e}")
                return None

    finally:
        if not sem_released:
            request_sem.release()


if __name__ == "__main__":
    cli()
