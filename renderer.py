import asyncio
import json
from loguru import logger
import click
import httpx
from pathlib import Path



class Renderer:
    def __init__(self, *, endpoint: str, data_dir: str, output_dir: str) -> None:
        self._endpoint = endpoint
        self._data_dir = Path(data_dir)
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def render(self) -> None:
        """Render the .ply and .glb files using the renderer endpoint."""
        click.echo(f"Rendering {self._data_dir} with endpoint {self._endpoint}", err=True)
        tasks: list[asyncio.Task] = []
        try:
            process_sem = asyncio.Semaphore(1)
            # Collect both .ply and .glb files
            ply_files = list(self._data_dir.glob("*.ply"))
            glb_files = list(self._data_dir.glob("*.glb"))
            all_files = ply_files + glb_files
            tasks = [
                asyncio.create_task(
                    self._process_prompt(
                        process_sem=process_sem,
                        file=file
                    )
                )
                for file in all_files
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
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
        """Render the .ply or .glb files using the renderer endpoint."""
        async with process_sem:
            click.echo(f"Rendering {file}...", err=True)
            try:
                timeout = httpx.Timeout(connect=300.0, read=300.0, write=300.0, pool=300.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    try:
                        with open(file, "rb") as f:
                            file_contents = f.read()
                        if file.name.endswith(".ply"):
                            endpoint = f"{self._endpoint}/render_ply" 
                        elif file.name.endswith(".glb"):
                            endpoint = f"{self._endpoint}/render_glb"
                        else:
                            raise ValueError(f"Unsupported file type: {file.name}")
                        response = await client.post(
                            endpoint,
                            files={"file": (file.name, file_contents, "application/octet-stream")},
                        )
                        response.raise_for_status()
                        content = response.content
                        output_file = self._output_dir / f"{file.name.split('.')[0]}.png"
                        with open(output_file, "wb") as f:
                            f.write(content)
                        click.echo(f"Rendered {file.name} to {output_file}", err=True)
                    except Exception as e:
                        logger.error(f"Renderer failed for file {file}: {e}")
                        click.echo(f"Renderer failed for file {file}: {e}", err=True)
            except Exception as e:
                logger.error(f"Renderer failed: {e}")
                click.echo(json.dumps({"success": False, "error": str(e)}), err=True)
                raise SystemExit(1)
