import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel, ConfigDict


STATUS_WARMING_UP = "warming_up"
STATUS_READY = "ready"
STATUS_GENERATING = "generating"
STATUS_COMPLETE = "complete"
STATUS_REPLACE = "replace"


class PromptItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    image_url: str
    stem: str | None = None


class Generator:
    """Generator client for the reference batch generation API."""

    def __init__(
        self,
        endpoint: str,
        seed: int,
        output_folder: Path,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.seed = seed
        self.output_folder = Path(output_folder)
        self.echo = echo or (lambda msg: None)
        self.output_folder.mkdir(parents=True, exist_ok=True)

    async def generate_all(self, prompts: list[PromptItem]) -> None:
        if not prompts:
            raise RuntimeError("No prompts provided")

        batch_prompts = [self._normalize_prompt(p) for p in prompts]
        stems = {item["stem"] for item in batch_prompts}
        if len(stems) != len(batch_prompts):
            raise RuntimeError("Prompt stems must be unique within a batch")

        self.echo(f"Submitting batch with {len(batch_prompts)} prompts...")

        timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await self._wait_until_ready(client)
            await self._submit_generate(client, batch_prompts)
            await self._wait_until_complete(client)
            zip_bytes = await self._download_results(client)

        saved, failed = self._extract_results_zip(zip_bytes)

        accounted = set(saved) | set(failed.keys())
        missing = sorted(stems - accounted)
        if failed:
            failed_json = json.dumps(failed, indent=2, sort_keys=True)
            raise RuntimeError(
                f"Generation failed for {len(failed)} prompts "
                f"(see failed.json / _failed.json in {self.output_folder}):\n{failed_json}"
            )
        if missing:
            raise RuntimeError(
                f"Missing generated outputs for stems: {', '.join(missing)}"
            )

        self.echo(
            f"Generation completed: saved {len(saved)} .js files to {self.output_folder}"
        )

    def _normalize_prompt(self, prompt: PromptItem) -> dict[str, str]:
        stem = prompt.stem or self._stem_from_url(prompt.image_url)
        stem = stem.strip().lower()
        if not stem.isalnum():
            raise RuntimeError(
                f"Invalid stem '{stem}'. Batch API requires lowercase alphanumeric stems."
            )
        return {"stem": stem, "image_url": prompt.image_url}

    def _stem_from_url(self, image_url: str) -> str:
        filename = image_url.split("/")[-1]
        return filename.split(".")[0]

    async def _wait_until_ready(self, client: httpx.AsyncClient) -> None:
        self.echo("Waiting for generator to become ready...")
        for poll in range(1, 181):
            payload = await self._get_status(client)
            status = payload.get("status")

            if status in (STATUS_READY, STATUS_COMPLETE):
                self.echo(f"Generator status is {status}; proceeding")
                return

            if status == STATUS_REPLACE:
                raise RuntimeError(
                    "Generator requested pod replacement (status=replace). "
                    "Restart the generator pod and retry."
                )

            if status not in (STATUS_WARMING_UP, STATUS_GENERATING):
                raise RuntimeError(f"Unexpected status during warmup: {status}")

            if status == STATUS_GENERATING:
                progress = payload.get("progress")
                total = payload.get("total")
                if progress is not None and total is not None:
                    self.echo(f"Generator currently busy: {progress}/{total}")

            await asyncio.sleep(1.0 if poll < 30 else 2.0)

        raise RuntimeError("Timed out waiting for generator readiness")

    async def _submit_generate(
        self, client: httpx.AsyncClient, prompts: list[dict[str, str]]
    ) -> None:
        request_body = {"prompts": prompts, "seed": self.seed}
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(
                    f"{self.endpoint}/generate", json=request_body
                )
                if response.status_code == 409:
                    detail = response.json()
                    current_status = detail.get("current_status", "unknown")
                    raise RuntimeError(
                        f"Generator rejected batch (current_status={current_status})"
                    )
                response.raise_for_status()
                payload = response.json()
                accepted = int(payload.get("accepted", 0))
                if accepted != len(prompts):
                    raise RuntimeError(
                        f"Batch accepted {accepted}/{len(prompts)} prompts"
                    )
                self.echo(f"Batch accepted: {accepted} prompts")
                return
            except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
                if attempt == max_attempts:
                    raise RuntimeError(f"Failed to submit batch: {exc}") from exc
                backoff = min(1.5 * (2 ** (attempt - 1)), 10.0)
                self.echo(
                    f"/generate attempt {attempt}/{max_attempts} failed ({exc}); retrying in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)

    async def _wait_until_complete(self, client: httpx.AsyncClient) -> None:
        self.echo("Polling generation status...")
        last_progress: tuple[int | None, int | None] | None = None

        for poll in range(1, 1201):
            payload = await self._get_status(client)
            status = payload.get("status")

            if status == STATUS_COMPLETE:
                self.echo("Generator reached complete")
                return

            if status == STATUS_REPLACE:
                raise RuntimeError(
                    "Generator requested pod replacement while generating (status=replace)."
                )

            if status == STATUS_GENERATING:
                progress = self._to_int_or_none(payload.get("progress"))
                total = self._to_int_or_none(payload.get("total"))
                current = (progress, total)
                if current != last_progress:
                    if progress is not None and total is not None:
                        self.echo(f"Generation progress: {progress}/{total}")
                    else:
                        self.echo("Generation in progress...")
                    last_progress = current
            elif status in (STATUS_READY, STATUS_WARMING_UP):
                self.echo(
                    f"Generator status changed to {status}; waiting for completion"
                )
            else:
                raise RuntimeError(f"Unexpected status while generating: {status}")

            await asyncio.sleep(1.0 if poll < 120 else 2.0)

        raise RuntimeError("Timed out waiting for generation completion")

    async def _download_results(self, client: httpx.AsyncClient) -> bytes:
        self.echo("Downloading batch results archive...")
        read_timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        total_size = 0
        chunks: list[bytes] = []

        async with client.stream(
            "GET", f"{self.endpoint}/results", timeout=read_timeout
        ) as response:
            if response.status_code == 409:
                detail = "results not available"
                try:
                    payload = json.loads((await response.aread()).decode("utf-8"))
                    if isinstance(payload, dict):
                        detail = str(payload.get("detail", detail))
                except Exception:
                    pass
                raise RuntimeError(f"/results rejected: {detail}")
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                chunks.append(chunk)
                total_size += len(chunk)

        self.echo(f"Downloaded ZIP archive ({total_size / 1024 / 1024:.2f} MiB)")
        return b"".join(chunks)

    async def _get_status(self, client: httpx.AsyncClient) -> dict:
        response = await client.get(f"{self.endpoint}/status")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid /status response shape")
        self.echo(
            "Status response: "
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )
        return payload

    def _extract_results_zip(self, zip_bytes: bytes) -> tuple[set[str], dict[str, str]]:
        saved: set[str] = set()
        failed: dict[str, str] = {}

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                for name in zf.namelist():
                    if Path(name).name == "_failed.json":
                        raw = zf.read(name)
                        (self.output_folder / "_failed.json").write_bytes(raw)
                        (self.output_folder / "failed.json").write_bytes(raw)
                        try:
                            failed_payload = json.loads(raw.decode("utf-8"))
                            if isinstance(failed_payload, dict):
                                failed = {
                                    str(k): str(v) for k, v in failed_payload.items()
                                }
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                        continue
                    if not name.endswith(".js"):
                        continue

                    stem = Path(name).stem
                    output_path = self.output_folder / f"{stem}.js"
                    output_path.write_bytes(zf.read(name))
                    saved.add(stem)
        except zipfile.BadZipFile as exc:
            raise RuntimeError("Received invalid ZIP archive from /results") from exc

        return saved, failed

    def _to_int_or_none(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
