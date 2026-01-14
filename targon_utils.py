import asyncio
from typing import Callable

import httpx
from loguru import logger
from targon.client.serverless import ServerlessResourceListItem

from targon_client import ContainerDeployConfig, TargonClient, TargonClientError


def _log(msg: str, echo: Callable[[str], None] | None, level: str = "info") -> None:
    """Log message using echo if provided, otherwise logger."""
    if echo:
        echo(msg)
    else:
        getattr(logger, level)(msg)


async def wait_for_visible(
    client: TargonClient,
    name: str,
    *,
    timeout: float,  # noqa: ASYNC109
    check_interval: float = 5.0,
    echo: Callable[[str], None] | None = None,
) -> ServerlessResourceListItem | None:
    """
    Wait for the container to become visible (stage 1).
    Targon containers may not appear immediately after deployment.
    """
    deadline = asyncio.get_running_loop().time() + timeout

    start = asyncio.get_running_loop().time()
    time_elapsed = asyncio.get_running_loop().time() - start
    while time_elapsed < timeout:
        container = await client.get_container(name)
        if container and container.url:
            return container

        if asyncio.get_running_loop().time() >= deadline:
            _log(f"Container {name} not visible within {timeout}s", echo, "warning")
            return None

        await asyncio.sleep(check_interval)
        time_elapsed = asyncio.get_running_loop().time() - start

    _log(f"Container {name} not visible within {timeout}s. Timeout reached.", echo, "warning")
    return None


async def wait_for_healthy(
    url: str,
    *,
    timeout: float,  # noqa: ASYNC109
    check_interval: float = 5.0,
    health_check_path: str = "/health",
    echo: Callable[[str], None] | None = None,
) -> bool:
    """Wait for the container health endpoint to return 200 (stage 2)."""
    # If health_check_path is a full URL, use it directly; otherwise append to container URL
    if health_check_path.startswith("http://") or health_check_path.startswith("https://"):
        health_url = health_check_path
    else:
        health_url = f"{url}{health_check_path}"

    async with httpx.AsyncClient(timeout=30.0) as http:
        start = asyncio.get_running_loop().time()
        time_elapsed = asyncio.get_running_loop().time() - start
        while time_elapsed < timeout:
            try:
                response = await http.get(health_url)
                response.raise_for_status()
                if response.status_code == 200:
                    _log(f"Container at {url} healthy", echo, "info")  
                    return True
            except Exception as e:
                _log(f"Container not ready yet: {time_elapsed:.1f}/{timeout:.1f}s", echo, "info")
                await asyncio.sleep(check_interval)
                time_elapsed = asyncio.get_running_loop().time() - start
    _log(f"Container at {url} not healthy within {timeout}s. Timeout reached.", echo, "error")
    return False

async def ensure_running_container(
    client: TargonClient,
    name: str,
    config: ContainerDeployConfig,
    *,
    deploy_timeout: float = 600.0,
    warmup_timeout: float = 3600.0,
    check_interval: float = 10.0,
    health_check_path: str = "/health",
    echo: Callable[[str], None] | None = None,
) -> ServerlessResourceListItem | None:
    """
    Ensure a healthy container is running.

    Stages:
    1. Wait for the container to become visible (Targon-specific delay)
    2. Wait for a health check to pass

    Returns container if successful, None if failed.
    """
    # Deploy
    deploy_start = asyncio.get_running_loop().time()
    try:
        _log(f"Deploying container with config.", echo)
        await client.deploy_container(name, config)
    except TargonClientError:
        return None

    _log(f"Waiting for container to become visible.", echo)
    container = await wait_for_visible(
        client,
        name,
        timeout=deploy_timeout,
        check_interval=check_interval,
        echo=echo,
    )
    if not container:
        _log(f"Container failed to become visible", echo, "error")
        return None

    deploy_time = asyncio.get_running_loop().time() - deploy_start
    _log(f"Container ({container.uid}) visible in {deploy_time:.1f}s", echo)
    _log(f"Container URL: {container.url}", echo)
    _log(f"Health check path: {health_check_path}", echo)

    warmup_start = asyncio.get_running_loop().time()
    _log(f"Waiting {warmup_timeout}s for container to become healthy.", echo)
    if not await wait_for_healthy(
        container.url,
        timeout=warmup_timeout,
        check_interval=check_interval,
        health_check_path=health_check_path,
        echo=echo,
    ):
        _log(f"Container failed health check, deleting", echo, "error")
        await client.delete_container(container.uid)
        return None

    warmup_time = asyncio.get_running_loop().time() - warmup_start
    _log(f"Container healthy in {warmup_time:.1f}s", echo)

    return container
