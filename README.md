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
  --hash <your_commit_sha> \
  --wallet.name <wallet> \
  --wallet.hotkey <hotkey>
```

### Commit repository reference of your solution
```bash
python commit.py commit-repo \
  --repo <reference_to_your_repo> \
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
git log --oneline -1
# → a1b2c3d4 ← this is your commit SHA

# 2. Submit the hash to claim your timestamp
python commit.py commit-hash --hash a1b2c3d4 --wallet.name miner --wallet.hotkey default

# 3. Make your repo accessible to validators

# 4. Submit the repo reference
python commit.py commit-repo --repo https://github.com/you/your-solution --wallet.name miner --wallet.hotkey default
```

### What Breaks the Hash

Avoid these after submitting — they create new commits with different SHAs:

- `git commit --amend`
- `git rebase`
- Cherry-picking into a different repo
- Re-committing the same files (different timestamp = different hash)