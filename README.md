# 404-gen-commit

Command line tool for the 404 subnet to submit miner solutions.

## Installation

### Option 1: Install as a CLI tool (Recommended)

Install the package in editable mode to use the `404-cli` command directly:

```bash
pip install -e .
```

After installation, you can use `404-cli` directly without `python`:

```bash
404-cli --help
404-cli commit-hash --hash <hash> --wallet.name <name> --wallet.hotkey <hotkey>
```

### Option 2: Install dependencies only

If you prefer to use `python commit.py` instead:

```bash
pip install -r requirements.txt
```

## Usage

### Commit hash of your solution
```bash
404-cli commit-hash \
  --hash <full_40_char_commit_sha> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

Or using `python commit.py`:
```bash
python commit.py commit-hash \
  --hash <full_40_char_commit_sha> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

### Commit repository reference and CDN URL
```bash
404-cli commit-repo-cdn \
  --repo <owner/repo-name> \
  --cdn-url <s3-compatible-storage-url> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

Or using `python commit.py`:
```bash
python commit.py commit-repo-cdn \
  --repo <owner/repo-name> \
  --cdn-url <s3-compatible-storage-url> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

The `commit-repo-cdn` command commits both the repository reference and CDN URL in a single transaction. Before committing, it validates that the CDN URL is accessible by performing a HEAD request.

**Example:**
```bash
404-cli commit-repo-cdn \
  --repo your-username/your-solution \
  --cdn-url https://my-bucket.s3.amazonaws.com/models/ \
  --wallet.name miner \
  --wallet.hotkey default
```

**Output:**
On success, outputs JSON:
```json
{"success": true, "repo": "your-username/your-solution", "cdn_url": "https://my-bucket.s3.amazonaws.com/models/"}
```

On failure (if CDN URL is not accessible), outputs error JSON:
```json
{"success": false, "error": "CDN URL https://my-bucket.s3.amazonaws.com/models/ is not accessible: 404"}
```

**Notes:**
- Both `--repo` and `--cdn-url` are required
- The command validates CDN URL accessibility before committing
- Uses the same wallet and network options as other commit commands

### List all commitments
```bash
404-cli list-all
```

Or using `python commit.py`:
```bash
python commit.py list-all
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--wallet.name` | required | Wallet name |
| `--wallet.hotkey` | required | Wallet hotkey |
| `--wallet.path` | ~/.bittensor | Path to wallet |
| `--subtensor.endpoint` | finney | Subtensor network |
| `--netuid` | 17 | Subnet UID |
| `-v` | | Verbosity: -v INFO, -vv DEBUG |

## Why Two-Step Submission?

The 404 subnet uses a "king of the hill" competition where submission timing matters — earlier submissions gain priority. We use a **commit-reveal scheme** with Git's content-addressable hashing:

1. **Commit phase**: Submit only your git commit SHA (a cryptographic hash of your code)
2. **Reveal phase**: Submit your repository reference so validators can fetch and evaluate your code

The block when you call `commit-hash` determines your submission timestamp.

### Why This Works

Git commit hashes are deterministic — derived from your code, commit message, author info, and timestamps. You cannot find different code that produces the same hash. This means:

- Your hash **commits** you to specific code before anyone sees it
- When you reveal the repo, validators verify the hash matches

### Recommended Workflow
```bash
# 1. Create your solution in a PRIVATE repository
git add . && git commit -m "My solution"
git log --format="%H" -1
# → a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 ← full 40-char SHA

# 2. Submit the hash to claim your timestamp
404-cli commit-hash \
  --hash a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 \
  --wallet.name miner \
  --wallet.hotkey default

# 3. Make your repo accessible to validators

# 4. Submit the repo reference and CDN URL
404-cli commit-repo-cdn \
  --repo your-username/your-solution \
  --cdn-url https://my-bucket.s3.amazonaws.com/models/ \
  --wallet.name miner \
  --wallet.hotkey default
```

### What Breaks the Hash

Avoid these after submitting — they create new commits with different SHAs:

- `git commit --amend`
- `git rebase`
- Cherry-picking into a different repo
- Re-committing the same files (different timestamp = different hash)


## Utility commands

### Start Generator

The `start-generator` command deploys and starts a generator container on Targon. It deploys the container and outputs the container URL which can then be used with the `generate` command.

**Options:**
- `--image-url` (required): URL of the Docker image to deploy
- `--targon-api-key` (required): Targon API key for authentication

**Example:**
```bash
404-cli start-generator \
  --image-url docker.io/username/model-generator:v1.0.0 \
  --targon-api-key your-targon-api-key-here
```

**Output:**
On success, outputs JSON with the container URL:
```json
{"success": true, "container_url": "https://generator-abc123.targon.io"}
```

The container URL is also displayed on stderr and should be used as the `--endpoint` parameter for the `generate` command.

### Generate Models

The `generate` command processes a list of prompt images and generates 3D models (.ply files) using a generator endpoint. It:
1. Reads prompts (image URLs) from a text file
2. Downloads each prompt image from its URL
3. Generates a 3D model for each prompt using the generator endpoint
4. Saves all generated models as .ply files to the local filesystem

**Options:**
- `--prompts-file` (required): Path to a text file containing one image URL per line
- `--endpoint` (required): Generator endpoint URL (obtained from `start-generator` command)
- `--seed` (required): Seed value for generation (ensures reproducibility)
- `--output-folder` (optional, default: "results"): Local folder path where generated .ply files will be saved

**Example:**

First, start the generator container:
```bash
404-cli start-generator \
  --image-url docker.io/username/model-generator:v1.0.0 \
  --targon-api-key your-targon-api-key-here
```

Note the container URL from the output (e.g., `https://generator-abc123.targon.io`).

Then, create a file `prompts.txt` with image URLs.

Run the generate command:
```bash
404-cli generate \
  --prompts-file prompts.txt \
  --endpoint https://generator-abc123.targon.io \
  --seed 42 \
  --output-folder results
```

The generated files will be saved locally at paths like:
- `results/22de4efc4723f624b92889e8c79c9b4fb903e8a6b5907c9f0727ede8f2ccab47.ply`
- `results/8c6c463fe4d3d9ed969a71ca8171b2571bb14f5fae057cf12d3743014d46c747.ply`
- etc.

**Output:**
On success, outputs JSON:
```json
{"success": true}
```

On failure, outputs error JSON:
```json
{"success": false, "error": "Error message here"}
```

**Notes:**
- Prompts are processed with concurrency control to limit resource usage
- Each generation attempt includes automatic retries (up to 3 attempts) with exponential backoff
- Generation progress and status messages are output to stderr, while JSON results go to stdout
- The output folder is created automatically if it doesn't exist

### Start Renderer

The `start-renderer` command deploys and starts a renderer container on Targon. It deploys the container using the predefined renderer image and outputs the container URL which can then be used with the `render` command.

**Options:**
- `--targon-api-key` (required): Targon API key for authentication

**Example:**
```bash
404-cli start-renderer \
  --targon-api-key your-targon-api-key-here
```

**Output:**
On success, outputs JSON with the container URL:
```json
{"success": true, "container_url": "https://render-abc123.targon.io"}
```

The container URL is also displayed on stderr and should be used as the `--endpoint` parameter for the `render` command.

**Notes:**
- Uses the predefined image: `ghcr.io/404-repo/render-service:latest`
- Deploys on `rtx4090-small` resource type
- Uses port 8000 and health check path `/health`

### Render Models

The `render` command processes .ply files and renders them to PNG images using a renderer endpoint. It:
1. Scans the specified directory for all .ply and .glb files
2. Sends each .ply and .glb file to the renderer endpoints
3. Saves the rendered PNG images to the output directory

**Options:**
- `--data-dir` (required): Path to the directory containing the .ply files to render
- `--endpoint` (required): Renderer endpoint URL (obtained from `start-renderer` command)
- `--output-dir` (optional, default: "results"): Path to the directory where rendered PNG images will be saved

**Example:**

First, start the renderer container:
```bash
404-cli start-renderer \
  --targon-api-key your-targon-api-key-here
```

Note the container URL from the output (e.g., `https://render-abc123.targon.io`).

Then, render the .ply files:
```bash
404-cli render \
  --data-dir results \
  --endpoint https://render-abc123.targon.io \
  --output-dir images
```

The rendered files will be saved locally at paths like:
- `images/22de4efc4723f624b92889e8c79c9b4fb903e8a6b5907c9f0727ede8f2ccab47.png`
- `images/8c6c463fe4d3d9ed969a71ca8171b2571bb14f5fae057cf12d3743014d46c747.png`
- etc.

**Output:**
On success, outputs JSON:
```json
{"success": true, "output_dir": "images"}
```

On failure, outputs error JSON:
```json
{"success": false, "error": "Error message here"}
```

**Notes:**
- Files are processed with concurrency control (up to 2 concurrent renders)
- Each .ply file is rendered to a PNG with the same base filename
- The output directory is created automatically if it doesn't exist
- Render progress and status messages are output to stderr, while JSON results go to stdout

### Stop Pods

The `stop-pods` command stops all running generator, render, and judge containers on Targon. This is useful for cleaning up resources after completing generation tasks.

**Options:**
- `--targon-api-key` (required): Targon API key for authentication

**Example:**
```bash
404-cli stop-pods \
  --targon-api-key your-targon-api-key-here
```

**Output:**
The command outputs status messages to stderr as it stops each container. No JSON output is produced on success.

**Notes:**
- Only containers with names matching "generator", "render", or "judge" are stopped
- If a container is already stopped, it will be skipped

### Start Judge

The `start-judge` command deploys and starts a judge container on Targon. It deploys the container using the predefined vLLM image with the GLM-4.1V vision model and outputs the container URL which can then be used with the `judge` command.

**Options:**
- `--targon-api-key` (required): Targon API key for authentication

**Example:**
```bash
404-cli start-judge \
  --targon-api-key your-targon-api-key-here
```

**Output:**
On success, outputs JSON with the container URL:
```json
{"success": true, "container_url": "https://serv-u-1325748-s7qx21sp30zou6ik.serverless.targon.com"}
```

The container URL is also displayed on stderr and should be used as the `--endpoint` parameter for the `judge` command.

**Notes:**
- Uses the predefined image: `vllm/vllm-openai:latest`
- Deploys on `rtx4090-small` resource type
- Uses port 8000 and health check path `/health`
- Runs the GLM-4.1V-9B-Thinking vision model for evaluating 3D models

### Judge Models

The `judge` command evaluates two sets of rendered 3D model images against their prompt images using a vision language model (VLM). It:
1. Reads prompt image URLs from a text file
2. Loads corresponding rendered images from two directories (image_dir_1 and image_dir_2)
3. For each prompt, runs a position-balanced duel evaluation (two calls with swapped model positions)
4. Calculates average penalties to determine the winner
5. Saves all duel results to a JSON file

**Options:**
- `--prompt-file` (required): Path to a text file containing one prompt image URL per line
- `--image-dir-1` (required): Path to the directory containing the first set of rendered PNG images
- `--image-dir-2` (required): Path to the directory containing the second set of rendered PNG images
- `--endpoint` (required): Judge endpoint URL (obtained from `start-judge` command)
- `--seed` (required): Seed value for evaluation (ensures reproducibility)
- `--output-file` (optional, default: "duels.json"): Path to the JSON file where duel results will be saved

**Important:** The image filenames in both directories must match the prompt key (derived from the URL filename). For example, if the prompt URL is `https://sn12domain.org/fb99ec66676f56b5b905b1859db0bc2ea430658fb66903faac86243cdff29ba6.png`, then both `image-dir-1` and `image-dir-2` must contain a file named `fb99ec66676f56b5b905b1859db0bc2ea430658fb66903faac86243cdff29ba6.png` for the duel to work correctly.

**Example:**

First, start the judge container:
```bash
404-cli start-judge \
  --targon-api-key your-targon-api-key-here
```

Note the container URL from the output (e.g., `https://serv-u-1325748-s7qx21sp30zou6ik.serverless.targon.com`).

Then, create a file `prompts.txt` with prompt image URLs (one per line):
```
https://sn12domain.org/fb99ec66676f56b5b905b1859db0bc2ea430658fb66903faac86243cdff29ba6.png
https://sn12domain.org/fa800ddb1f6a05bb57d7cf994b167b2656139aa030e22eb9441cb74cae76a80b.png
```

Ensure you have rendered images in two directories (e.g., `images` and `images_2`) with filenames matching the prompt keys (derived from the URL filenames).

Run the judge command:
```bash
404-cli judge \
  --prompt-file prompts.txt \
  --image-dir-1 images \
  --image-dir-2 images_2 \
  --endpoint https://serv-u-1325748-s7qx21sp30zou6ik.serverless.targon.com \
  --seed 12345 \
  --output-file duels.json
```

**Output:**
On success, outputs JSON:
```json
{"success": true, "output_file": "duels.json"}
```

The duel results are saved to the specified JSON file (default: `duels.json`) with the following structure:
```json
{
  "fb99ec66676f56b5b905b1859db0bc2ea430658fb66903faac86243cdff29ba6": {
    "outcome": 0,
    "issues": "First model has minor shape differences and some detail inconsistencies compared to the prompt, second model matches perfectly"
  },
  "fa800ddb1f6a05bb57d7cf994b167b2656139aa030e22eb9441cb74cae76a80b": {
    "outcome": -1,
    "issues": "First model matches better than second model"
  }
}
```

Where:
- Keys are prompt names (derived from the URL filenames)
- `outcome`: `-1` = first model wins, `0` = draw, `1` = second model wins
- `issues`: Human-readable summary of the evaluation

On failure, outputs error JSON:
```json
{"success": false, "error": "Error message here"}
```

**Notes:**
- Uses position-balanced evaluation: each prompt is evaluated twice with swapped model positions to reduce position bias
- The judge uses a vision language model (GLM-4.1V-9B-Thinking) to compare rendered 3D model images against the prompt
- Image files must be PNG format and filenames must match the prompt keys (derived from URL filenames)
- Evaluation progress and judge responses are output to stderr, while JSON results go to stdout
- The output file is created automatically if it doesn't exist
