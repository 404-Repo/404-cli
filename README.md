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

The `generate` command submits a prompt batch to the generator service and downloads generated `.js` files. It:
1. Reads prompts from a JSON file (`--prompts-json`) with prompt objects containing `image_url` and optional `stem`
2. Submits the full prompt batch to `/generate`
3. Polls `/status` until generation reaches `complete`
4. Downloads `/results` ZIP and saves generated `.js` files locally

**Options:**
- `--prompts-json` (required): Path to JSON file with format:
  ```json
  {
    "prompts": [
      {"stem": "a1b2c3d4", "image_url": "https://storage.example.com/prompts/a1b2c3d4.jpg"},
      {"stem": "e5f6g7h8", "image_url": "https://storage.example.com/prompts/e5f6g7h8.png"}
    ],
    "seed": 42
  }
  ```
- `--endpoint` (required): Generator endpoint URL (obtained from `start-generator` command)
- `--seed` (optional): Seed value override. If omitted, uses `seed` from the prompts JSON
- `--output-folder` (optional, default: "results"): Local folder path where generated .js files will be saved

**Example:**

First, start the generator container:
```bash
404-cli start-generator \
  --image-url docker.io/username/model-generator:v1.0.0 \
  --targon-api-key your-targon-api-key-here
```

Note the container URL from the output (e.g., `https://generator-abc123.targon.io`).

Then, create a prompts JSON file (for example `example_prompts.json`).

Run the generate command:
```bash
404-cli generate \
  --prompts-json example_prompts.json \
  --endpoint https://generator-abc123.targon.io \
  --output-folder results
```

When `stem` is provided in JSON, output files are named `<stem>.js`.

The generated files will be saved locally at paths like:
- `results/22de4efc4723f624b92889e8c79c9b4fb903e8a6b5907c9f0727ede8f2ccab47.js`
- `results/8c6c463fe4d3d9ed969a71ca8171b2571bb14f5fae057cf12d3743014d46c747.js`
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
- Uses the predefined image: `europe-west3-docker.pkg.dev/gen-456515/active-competition/render-service-js:0.4.3`
- Deploys on `cpu-small` resource type
- Uses port 8000 and health check path `/health`

### Render Models

The `render` command processes JavaScript submission files (`.js`) and renders them to PNG images using a renderer endpoint. It:
1. Scans the specified directory for all `.js` files
2. Sends each file as JSON `{ "source": "<js code>" }` to the renderer endpoint (`/render/grid`)
3. Saves the rendered PNG images to the output directory

**Options:**
- `--data-dir` (required): Path to the directory containing the `.js` submission files to render
- `--endpoint` (required): Renderer endpoint URL (obtained from `start-renderer` command)
- `--output-dir` (optional, default: "results"): Path to the directory where rendered PNG images will be saved

**Example:**

First, start the renderer container:
```bash
404-cli start-renderer \
  --targon-api-key your-targon-api-key-here
```

Note the container URL from the output (e.g., `https://render-abc123.targon.io`).

Then, render the `.js` files:
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
- Each .js file is rendered to a PNG with the same base filename
- The output directory is created automatically if it doesn't exist
- Render progress and status messages are output to stderr, while JSON results go to stdout

### Stop Pods

The `stop-pods` command stops all running generator and render containers on Targon. This is useful for cleaning up resources after completing generation tasks.

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
- Only containers with names matching "generator" or "render" are stopped
- If a container is already stopped, it will be skipped
