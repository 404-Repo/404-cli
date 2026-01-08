# 404-gen-commit

Command line tool for the 404 subnet to submit miner solutions.

## Installation
```bash
pip install -r requirements.txt
```

## Usage

### Commit hash of your solution
```bash
python commit.py commit-hash \
  --hash <full_40_char_commit_sha> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

### Commit repository reference of your solution
```bash
python commit.py commit-repo \
  --repo <owner/repo-name> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

### List all commitments
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
python commit.py commit-hash \
  --hash a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 \
  --wallet.name miner \
  --wallet.hotkey default

# 3. Make your repo accessible to validators

# 4. Submit the repo reference
python commit.py commit-repo \
  --repo your-username/your-solution \
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

### Check Image

The `check-image` command verifies that a Docker image is accessible and can be deployed on Targon. It deploys a temporary container, verifies it's running, and then cleans it up. This is useful for validating Docker images before using them in batch generation.

**Options:**
- `--image-url` (required): URL of the Docker image to check
- `--targon-api-key` (required): Targon API key for authentication

**Example:**
```bash
python commit.py check-image \
  --image-url docker.io/username/model-generator:v1.0.0 \
  --targon-api-key your-targon-api-key-here
```

### Generate Models

The `generate` command processes a list of prompt images and generates 3D models (.ply files) using Targon containers. It:
1. Reads prompts (image URLs) from a text file
2. Deploys a Targon container with the specified Docker image
3. Downloads each prompt image from its URL
4. Generates a 3D model for each prompt using the container
5. Uploads all generated models to S3/R2 storage
6. Cleans up the container when finished

**Options:**
- `--prompts-file` (required): Path to a text file containing one image URL per line
- `--image-url` (required): URL of the Docker image to use for generation
- `--targon-api-key` (required): Targon API key for authentication
- `--s3-access-key-id` (required): S3/R2 access key ID
- `--s3-secret-access-key` (required): S3/R2 secret access key
- `--s3-bucket-name` (required): S3/R2 bucket name where generated models will be saved
- `--s3-url` (required): S3/R2 endpoint URL
- `--seed` (required): Seed value for generation (ensures reproducibility)
- `--folder` (optional, default: "results"): Folder path in the S3 bucket where models will be saved

**Example:**

First, create a file `prompts.txt` with image URLs:
```text
https://domain.org/22de4efc4723f624b92889e8c79c9b4fb903e8a6b5907c9f0727ede8f2ccab47.png
https://domain.org/8c6c463fe4d3d9ed969a71ca8171b2571bb14f5fae057cf12d3743014d46c747.png
https://domain.org/a7da89058a9f913d0012099401e7e271fa02d0e660bd560955b18c1cdc761370.png
```

Then run the generate command:
```bash
python commit.py generate \
  --prompts-file prompts.txt \
  --image-url docker.io/username/model-generator:v1.0.0 \
  --targon-api-key your-targon-api-key-here \
  --s3-access-key-id your-access-key-id \
  --s3-secret-access-key your-secret-access-key \
  --s3-bucket-name my-bucket \
  --s3-url https://your-account-id.r2.cloudflarestorage.com \
  --seed 42 \
  --folder results
```

The generated files will be saved to S3 at paths like:
- `results/22de4efc4723f624b92889e8c79c9b4fb903e8a6b5907c9f0727ede8f2ccab47.ply`
- `results/8c6c463fe4d3d9ed969a71ca8171b2571bb14f5fae057cf12d3743014d46c747.ply`
- etc.
