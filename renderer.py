import asyncio
import base64
import binascii
import json
from pathlib import Path

import click
import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict


_RENDER_PATH: str = "/render"
_RENDER_GRID_PATH: str = "/render/grid"
_MAX_ATTEMPTS: int = 3


class View(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    theta: float
    phi: float


# 8 white-bg views: 4 front-ish and 4 diagnostic views.
WHITE_VIEWS: list[View] = [
    View(name="front", theta=0, phi=0),
    View(name="front_left", theta=30, phi=0),
    View(name="front_right", theta=330, phi=0),
    View(name="front_above", theta=0, phi=-30),
    View(name="right", theta=90, phi=0),
    View(name="back", theta=180, phi=0),
    View(name="left", theta=270, phi=0),
    View(name="top_down", theta=0, phi=-90),
]

# 4 gray-bg views: front-ish set for fallback/rescue flow.
GRAY_VIEWS: list[View] = [
    View(name="front", theta=0, phi=0),
    View(name="front_left", theta=30, phi=0),
    View(name="front_right", theta=330, phi=0),
    View(name="front_above", theta=0, phi=-30),
]

WHITE_BG: str = "ffffff"
GRAY_BG: str = "808080"

_RENDER_DEFAULTS: dict[str, str] = {
    "lighting": "follow",
    "img_size": "1024",
    "cam_radius": "2.0",
    "cam_fov_deg": "49.1",
}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in (429, 430)
    return isinstance(exc, (httpx.TimeoutException, httpx.RequestError))


def _build_query(views: list[View], bg_color: str) -> dict[str, str]:
    return {
        **_RENDER_DEFAULTS,
        "thetas": ",".join(str(v.theta) for v in views),
        "phis": ",".join(str(v.phi) for v in views),
        "bg_color": bg_color,
    }


def _decode_render_response(
    response: httpx.Response, expected_views: int
) -> list[bytes]:
    if expected_views == 1:
        return [response.content]

    data = response.json()
    encoded = data.get("images")
    if not isinstance(encoded, list) or len(encoded) != expected_views:
        raise ValueError(
            f"render returned {len(encoded) if isinstance(encoded, list) else 'no'} images, "
            f"expected {expected_views}"
        )

    try:
        return [base64.b64decode(img) for img in encoded]
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"render returned invalid base64 images: {e}") from e


class Renderer:
    def __init__(self, *, endpoint: str, data_dir: str, output_dir: str) -> None:
        self._endpoint = endpoint.strip().rstrip("/")
        self._data_dir = Path(data_dir)
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def render(self) -> None:
        """Render .js files to 12 PNG views using POST /render."""
        click.echo(
            f"Rendering {self._data_dir} with endpoint {self._endpoint}", err=True
        )
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
                asyncio.create_task(
                    self._process_prompt(process_sem=process_sem, file=file)
                )
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

    async def _render_views(
        self,
        *,
        client: httpx.AsyncClient,
        source: str,
        views: list[View],
        bg_color: str,
    ) -> dict[str, bytes]:
        endpoint = f"{self._endpoint}{_RENDER_PATH}"
        response = await client.post(
            endpoint,
            params=_build_query(views, bg_color),
            json={"source": source},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        images = _decode_render_response(response, len(views))
        return {view.name: image for view, image in zip(views, images, strict=True)}

    async def _render_grid(self, *, client: httpx.AsyncClient, source: str) -> bytes:
        endpoint = f"{self._endpoint}{_RENDER_GRID_PATH}"
        response = await client.post(
            endpoint,
            json={"source": source},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.content

    async def _process_prompt(
        self, *, process_sem: asyncio.Semaphore, file: Path
    ) -> None:
        """Render one .js submission file into white/gray folders plus grid.png."""
        async with process_sem:
            click.echo(f"Rendering {file}...", err=True)
            timeout = httpx.Timeout(connect=300.0, read=300.0, write=300.0, pool=300.0)

            with open(file, "r", encoding="utf-8") as f:
                source = f.read()

            prompt_dir = self._output_dir / file.stem
            white_dir = prompt_dir / "white"
            gray_dir = prompt_dir / "gray"
            white_dir.mkdir(parents=True, exist_ok=True)
            gray_dir.mkdir(parents=True, exist_ok=True)

            last_error: Exception | None = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        white_images, gray_images, grid_image = await asyncio.gather(
                            self._render_views(
                                client=client,
                                source=source,
                                views=WHITE_VIEWS,
                                bg_color=WHITE_BG,
                            ),
                            self._render_views(
                                client=client,
                                source=source,
                                views=GRAY_VIEWS,
                                bg_color=GRAY_BG,
                            ),
                            self._render_grid(
                                client=client,
                                source=source,
                            ),
                        )

                        for view in WHITE_VIEWS:
                            output_file = white_dir / f"{view.name}.png"
                            with open(output_file, "wb") as out:
                                out.write(white_images[view.name])

                        for view in GRAY_VIEWS:
                            output_file = gray_dir / f"{view.name}.png"
                            with open(output_file, "wb") as out:
                                out.write(gray_images[view.name])

                        grid_output_file = prompt_dir / "grid.png"
                        with open(grid_output_file, "wb") as out:
                            out.write(grid_image)

                        click.echo(
                            f"Rendered {file.name} to {prompt_dir} "
                            f"(white={len(WHITE_VIEWS)}, gray={len(GRAY_VIEWS)}, grid=1)",
                            err=True,
                        )
                        return
                except Exception as e:
                    last_error = e
                    retryable = _is_retryable(e)
                    if isinstance(e, httpx.HTTPStatusError):
                        response_text = (
                            e.response.text[:1000] if e.response is not None else ""
                        )
                        msg = (
                            f"Renderer HTTP error for file {file}: "
                            f"{e.response.status_code if e.response is not None else 'unknown status'} "
                            f"from render endpoints. Response: {response_text!r} "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                        )
                    else:
                        msg = (
                            f"Renderer request error for file {file}: "
                            f"{type(e).__name__}: {e!r} while calling render endpoints "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                        )
                    logger.error(msg)
                    click.echo(msg, err=True)
                    if retryable and attempt < _MAX_ATTEMPTS:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    break

            raise RuntimeError(f"Renderer failed for file {file}: {last_error!r}")
