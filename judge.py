from pathlib import Path
from pybase64 import b64encode
import httpx
import click
import asyncio
import json
from pydantic import BaseModel, Field
from typing import Literal
from openai import AsyncOpenAI


SYSTEM_PROMPT = """
You are a specialized 3D model evaluation system. 
Analyze visual quality and prompt adherence with expert precision. 
Always respond with valid JSON only."""
USER_PROMPT_IMAGE = """Does each 3D model match the image prompt?

Penalty 0-10:
0 = Perfect match
3 = Minor issues (slight shape differences, missing small details)
5 = Moderate issues (wrong style, significant details missing)
7 = Major issues (wrong category but related, e.g. chair vs stool)
10 = Completely wrong object

Output: {"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief>"}"""


class JudgeResponse(BaseModel):
    """Response from a judge evaluating a duel between two models."""

    penalty_1: int
    """Penalty for the first model (0-10, lower is better)."""
    penalty_2: int
    """Penalty for the second model (0-10, lower is better)."""
    issues: str
    """Human-readable issue summary produced by the judge."""


class DuelResult(BaseModel):
    """Result of a position-balanced duel evaluation."""

    outcome: Literal[-1, 0, 1] = Field(..., description="Duel outcome: -1 = left wins, 0 = draw, 1 = right wins")
    issues: str = Field(..., description="Human-readable issue summary from judge")


class Judge:

    def __init__(
        self,
        endpoint: str,
        model: str,
        seed: int,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.seed = seed
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    async def judge(self, prompt_file: Path, image_dir_1: Path, image_dir_2: Path, output_file: Path) -> None:
        # Validate all paths exist
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file {prompt_file} does not exist")
        if not prompt_file.is_file():
            raise ValueError(f"Prompt file {prompt_file} is not a file")
        with open(prompt_file, "r") as f:
            prompts = [line.strip() for line in f.readlines() if line.strip()]
            prompt_to_url = {prompt.split("/")[-1].split(".")[0]: prompt for prompt in prompts}
        
        if not image_dir_1.exists():
            raise FileNotFoundError(f"Image directory {image_dir_1} does not exist")
        if not image_dir_1.is_dir():
            raise ValueError(f"Image directory {image_dir_1} is not a directory")
        prompt_to_file_1 = {image_file.name.split(".")[0]: image_file for image_file in image_dir_1.glob("*.png")}
        
        if not image_dir_2.exists():
            raise FileNotFoundError(f"Image directory {image_dir_2} does not exist")
        if not image_dir_2.is_dir():
            raise ValueError(f"Image directory {image_dir_2} is not a directory")
        prompt_to_file_2 = {image_file.name.split(".")[0]: image_file for image_file in image_dir_2.glob("*.png")}
        
        tasks = []
        prompt_keys = []  # Track which prompt key corresponds to each task
        try:
            request_sem = asyncio.Semaphore(8)  # Using semaphores to limit request to one at a time.
            process_sem = asyncio.Semaphore(1)  # Limiting request to control traffic
            for prompt in prompt_to_url:
                if prompt not in prompt_to_file_1 or prompt not in prompt_to_file_2:
                    click.echo(f"No files for prompt {prompt} found in image directories", err=True)
                    continue
                task = asyncio.create_task(
                    self._process_prompt(
                        request_sem=request_sem,
                        process_sem=process_sem,
                        prompt_name=prompt,
                        prompt_url=prompt_to_url[prompt],
                        file_1=prompt_to_file_1[prompt],
                        file_2=prompt_to_file_2[prompt],
                    )
                )
                tasks.append(task)
                prompt_keys.append(prompt)
            click.echo(f"Generated {len(tasks)} tasks", err=True)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Collect successful duel results
            duel_results = {}
            for prompt_key, result in zip(prompt_keys, results):
                if isinstance(result, Exception):
                    click.echo(f"Prompt {prompt_key} failed: {result}", err=True)
                    duel_results[prompt_key] = {
                        "outcome": 0,
                        "issues": f"Error: {str(result)}",
                        "error": True,
                    }
                elif isinstance(result, DuelResult):
                    duel_results[prompt_key] = result.model_dump()
                else:
                    click.echo(f"Prompt {prompt_key} returned unexpected result type: {type(result)}", err=True)
                    duel_results[prompt_key] = {
                        "outcome": 0,
                        "issues": f"Unexpected result type: {type(result)}",
                        "error": True,
                    }
            
            # Save results to JSON file
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "w") as f:
                json.dump(duel_results, f, indent=2)
            click.echo(f"Saved {len(duel_results)} duel results to {output_file}", err=True)
            
        except (KeyboardInterrupt, asyncio.CancelledError):
            click.echo("\nInterrupted by user. Cancelling tasks and cleaning up...", err=True)
            # Cancel all running tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Wait for tasks to be cancelled
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as e:
            click.echo(f"Generation failed: {e}", err=True)
            raise
        finally:
            click.echo("Generation completed", err=True)

    async def _process_prompt(
        self,
        *,
        request_sem: asyncio.Semaphore,
        process_sem: asyncio.Semaphore,
        prompt_name: str,
        prompt_url: str,
        file_1: Path,
        file_2: Path,
    ) -> DuelResult:
        try:
            # Get prompt image
            async with request_sem:
                click.echo(f"Requesting prompt {prompt_url}...", err=True)
                response = await httpx.AsyncClient().get(prompt_url)
                response.raise_for_status()
                prompt_image = response.content

            # Get file 1 image
            with open(file_1, "rb") as f:
                file_1_image = f.read()
            # Get file 2 image
            with open(file_2, "rb") as f:
                file_2_image = f.read()
            
            client = self._create_openai_client()
            click.echo(f"Processing prompt {prompt_name}...", err=True)
            
            # Run position-balanced duel (two calls with swapped order)
            result_1, result_2 = await asyncio.gather(
                self.ask_judge(process_sem, client, prompt_name, prompt_image, file_1_image, file_2_image, self.seed),
                self.ask_judge(process_sem, client, prompt_name, prompt_image, file_2_image, file_1_image, self.seed),
            )
            
            # Calculate average penalties (position-balanced)
            left_penalty = (result_1.penalty_1 + result_2.penalty_2) / 2
            right_penalty = (result_1.penalty_2 + result_2.penalty_1) / 2

            # Determine outcome
            if abs(left_penalty - right_penalty) <= 1:
                outcome: Literal[-1, 0, 1] = 0
            elif left_penalty < right_penalty:
                outcome = -1
            else:
                outcome = 1

            duel_result = DuelResult(
                outcome=outcome,
                issues=result_1.issues,
            )
            click.echo(f"Processing prompt {prompt_name} completed: {duel_result}", err=True)
            return duel_result

        except Exception as e:
            click.echo(f"Processing prompt {prompt_name} failed: {e}", err=True)
            raise

    async def ask_judge(
        self,
        process_sem: asyncio.Semaphore,
        client: AsyncOpenAI,
        prompt_name: str,
        prompt_image: bytes,
        left_image: bytes,
        right_image: bytes,
        seed: int,
    ) -> JudgeResponse:
        """Ask the judge to evaluate two models against a prompt image.
        
        Args:
            process_sem: Semaphore to control concurrent processing
            client: OpenAI client instance
            prompt_name: Name/key of the prompt
            prompt_image: Bytes of the prompt image
            left_image: Bytes of the first model image (4 views)
            right_image: Bytes of the second model image (4 views)
            seed: Random seed for reproducibility
            
        Returns:
            JudgeResponse with penalties and issues
        """
        async with process_sem:
            # Encode images as base64
            prompt_img_b64 = b64encode(prompt_image).decode("utf-8")
            left_img_b64 = b64encode(left_image).decode("utf-8")
            right_img_b64 = b64encode(right_image).decode("utf-8")
            
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Image prompt to generate 3D model:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{prompt_img_b64}"},
                        },
                        {"type": "text", "text": "First 3D model (4 different views):"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{left_img_b64}"}
                        },
                        {"type": "text", "text": "Second 3D model (4 different views):"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{right_img_b64}"},
                        },
                        {"type": "text", "text": USER_PROMPT_IMAGE},
                    ],
                },
            ]
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "judge-response",
                    "schema": JudgeResponse.model_json_schema(),
                },
            }

            completion = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=response_format,
                seed=seed,
            )

            result = JudgeResponse.model_validate_json(completion.choices[0].message.content)
            click.echo(f"Judge response for prompt {prompt_name}: {result}", err=True)
            return result

    def _create_openai_client(self) -> AsyncOpenAI:
        """Create a configured OpenAI client for judge VLM calls."""
        return AsyncOpenAI(
            base_url=self.endpoint,
            api_key="EMPTY",
            timeout=self.timeout,
            http_client=httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)),
        )

