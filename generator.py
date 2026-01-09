import asyncio
from pathlib import Path
from typing import Callable

import httpx


class Generator:
    """Generator for processing prompts and creating 3D models."""

    def __init__(
        self,
        endpoint: str,
        seed: int,
        output_folder: Path,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        """
        Initialize the Generator.

        Args:
            endpoint: Generator endpoint URL
            seed: Seed value for generation (ensures reproducibility)
            output_folder: Path to folder where .ply files will be saved
            echo: Optional callback function for logging messages
        """
        self.endpoint = endpoint
        self.seed = seed
        self.output_folder = Path(output_folder)
        self.echo = echo or (lambda msg: None)
        
        # Create output folder if it doesn't exist
        self.output_folder.mkdir(parents=True, exist_ok=True)

    async def generate_all(self, prompts: list[str]) -> None:
        """
        Generate models for all prompts.

        Args:
            prompts: List of image URLs to process

        Raises:
            RuntimeError: If generation fails
            KeyboardInterrupt: If interrupted by user
        """
        tasks = []
        try:
            self.echo(f"Processing {len(prompts)} prompts...")
            request_sem = asyncio.Semaphore(1)  # Using semaphores to limit request to one at a time.
            process_sem = asyncio.Semaphore(8)  # Limiting request to control traffic
            tasks = [
                asyncio.create_task(
                    self._process_prompt(
                        request_sem=request_sem,
                        process_sem=process_sem,
                        endpoint=self.endpoint,
                        prompt=prompt,
                    )
                )
                for prompt in prompts
            ]
            self.echo(f"Generated {len(tasks)} tasks")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for prompt, result in zip(prompts, results):
                if isinstance(result, Exception):
                    self.echo(f"Prompt {prompt} generation failed: {result}")
                else:
                    self.echo(f"Prompt {prompt} generation successful")
            self.echo("Generation completed")
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.echo("\nInterrupted by user. Cancelling tasks and cleaning up...")
            # Cancel all running tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Wait for tasks to be cancelled
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as e:
            self.echo(f"Generation failed: {e}")
            raise

    async def _process_prompt(
        self,
        *,
        request_sem: asyncio.Semaphore,
        process_sem: asyncio.Semaphore,
        endpoint: str,
        prompt: str,
    ) -> None:
        """
        Downloads a prompt image from a public URL, generates a 3D model from it, and saves the result locally.

        Args:
            request_sem: Semaphore to limit concurrent requests
            process_sem: Semaphore to limit concurrent processing
            endpoint: Targon container endpoint URL
            prompt: Image URL to process

        Raises:
            RuntimeError: If generation fails
        """
        prompt_key = prompt.split("/")[-1].split(".")[0]

        async with process_sem:
            # Download image from public URL
            timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
            self.echo(f"Downloading image from {prompt}...")
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(prompt)
                response.raise_for_status()
                image = response.content
                result = await self._generate_with_retries(
                    request_sem=request_sem,
                    endpoint=endpoint,
                    image=image,
                    prompt_key=prompt_key,
                )
                if result is not None:
                    self.echo(f"Prompt {prompt_key} generation successful")
                else:
                    self.echo(f"Prompt {prompt_key} generation failed")
                    raise RuntimeError(f"Prompt {prompt_key} generation failed")

            # Save .ply file to local filesystem
            output_path = self.output_folder / f"{prompt_key}.ply"
            output_path.write_bytes(result)
            self.echo(f"Saved {prompt_key}.ply to {output_path}")

    async def _generate_with_retries(
        self,
        *,
        request_sem: asyncio.Semaphore,
        endpoint: str,
        image: bytes,
        prompt_key: str,
    ) -> bytes | None:
        """
        Generates a 3D model from an image using the Targon container with retries.

        Args:
            request_sem: Semaphore to limit concurrent requests
            endpoint: Targon container endpoint URL
            image: Image bytes to process
            prompt_key: Unique identifier for the prompt

        Returns:
            Generated model bytes, or None if all attempts failed

        Raises:
            RuntimeError: If all retry attempts fail
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
                self.echo(
                    f"Prompt {prompt_key} generation attempt {attempt + 1}/{max_attempts} after {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)

            result = await self._generate_attempt(
                request_sem=request_sem,
                endpoint=endpoint,
                image=image,
                prompt_key=prompt_key,
            )

            if result is not None:  # None means retryable failure — continue to next attempt
                return result

        raise RuntimeError(f"Prompt {prompt_key} generation failed")

    async def _generate_attempt(
        self,
        *,
        request_sem: asyncio.Semaphore,
        endpoint: str,
        image: bytes,
        prompt_key: str,
    ) -> bytes | None:
        """
        Single generation attempt.

        Args:
            request_sem: Semaphore to limit concurrent requests
            endpoint: Targon container endpoint URL
            image: Image bytes to process
            prompt_key: Unique identifier for the prompt

        Returns:
            Generated model bytes, or None if the attempt failed
        """
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
                        data={"seed": self.seed},
                    ) as response:
                        response.raise_for_status()

                        elapsed = asyncio.get_running_loop().time() - start_time

                        request_sem.release()
                        sem_released = True

                        self.echo(f"Prompt {prompt_key} generation completed in {elapsed:.1f}s")

                        try:
                            content = await response.aread()
                        except Exception as e:
                            return None

                        download_time = asyncio.get_running_loop().time() - start_time - elapsed
                        mb_size = len(content) / 1024 / 1024
                        self.echo(
                            f"Prompt {prompt_key} generated in {elapsed:.1f}s, "
                            f"downloaded in {download_time:.1f}s, {mb_size:.1f} MiB"
                        )

                        return content

                except Exception as e:
                    self.echo(f"Prompt {prompt_key} generation failed: {e}")
                    return None

        finally:
            if not sem_released:
                request_sem.release()
