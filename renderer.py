import asyncio
import json
from pathlib import Path

import click
import httpx
from loguru import logger


_RENDER_GRID_PATH: str = "/render/grid"
_MAX_ATTEMPTS: int = 3


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in (429, 430)
    return isinstance(exc, (httpx.TimeoutException, httpx.RequestError))


class Renderer:
    def __init__(self, *, endpoint: str, data_dir: str, output_dir: str) -> None:
        self._endpoint = endpoint.strip().rstrip("/")
        self._data_dir = Path(data_dir)
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def render(self) -> None:
        """Render .js files by sending JSON to POST /render/grid."""
        click.echo(f"Rendering {self._data_dir} with endpoint {self._endpoint}", err=True)
        tasks: list[asyncio.Task] = []
        try:
            health_url = f"{self._endpoint}/health"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                    health_response = await client.get(health_url)
                click.echo(
                    f"Renderer health check: {health_url} -> {health_response.status_code}",
                    err=True,
                )
            except Exception as e:
                logger.warning(
                    f"Renderer health check failed for {health_url}: {type(e).__name__}: {e!r}"
                )
                click.echo(
                    f"Renderer health check failed for {health_url}: {type(e).__name__}: {e!r}",
                    err=True,
                )

            process_sem = asyncio.Semaphore(1)
            js_files = list(self._data_dir.glob("*.js"))
            if not js_files:
                raise RuntimeError(
                    f"No .js files found in {self._data_dir}. "
                    "This service expects JavaScript source and JSON body {'source': ...}."
                )

            tasks = [
                asyncio.create_task(self._process_prompt(process_sem=process_sem, file=file))
                for file in js_files
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [res for res in results if isinstance(res, Exception)]
            if failures:
                raise RuntimeError(f"Rendering failed for {len(failures)} file(s)")
        except KeyboardInterrupt:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise SystemExit(130)
        except Exception as e:
            logger.error(f"Renderer failed: {e}")
            click.echo(json.dumps({"success": False, "error": str(e)}))
            raise SystemExit(1)

    async def _process_prompt(self, *, process_sem: asyncio.Semaphore, file: Path) -> None:
        """Render one .js submission file via POST /render/grid."""
        async with process_sem:
            click.echo(f"Rendering {file}...", err=True)
            timeout = httpx.Timeout(connect=300.0, read=300.0, write=300.0, pool=300.0)
            endpoint = f"{self._endpoint}{_RENDER_GRID_PATH}"

            with open(file, "r", encoding="utf-8") as f:
                source = f.read()

            last_error: Exception | None = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(
                            endpoint,
                            json={"source": source},
                            headers={"Content-Type": "application/json"},
                        )
                        response.raise_for_status()
                        output_file = self._output_dir / f"{file.stem}.png"
                        with open(output_file, "wb") as out:
                            out.write(response.content)
                        click.echo(f"Rendered {file.name} to {output_file}", err=True)
                        return
                except Exception as e:
                    last_error = e
                    retryable = _is_retryable(e)
                    if isinstance(e, httpx.HTTPStatusError):
                        response_text = e.response.text[:1000] if e.response is not None else ""
                        msg = (
                            f"Renderer HTTP error for file {file}: "
                            f"{e.response.status_code if e.response is not None else 'unknown status'} "
                            f"from {endpoint}. Response: {response_text!r} "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                        )
                    else:
                        msg = (
                            f"Renderer request error for file {file}: "
                            f"{type(e).__name__}: {e!r} while calling {endpoint} "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                        )
                    logger.error(msg)
                    click.echo(msg, err=True)
                    if retryable and attempt < _MAX_ATTEMPTS:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    break

            raise RuntimeError(f"Renderer failed for file {file}: {last_error!r}")
