# 404-cli
Command Line Tool for the 404 subnet to submit miner solutions

## Installation
```bash
pip install -r requirements.txt
```

## Usage

### Create .env file
```
SUBTENSOR_ENDPOINT=finney
WALLET_NAME=miner
WALLET_HOTKEY=default
NETUID=17
```

### Commit hash of your solution
```bash
python main.py commit-hash --hash <your_commit_sha>
```

### Commit repository reference of your solution
```bash
python main.py commit-repo --repo <reference_to_your_repo>
```

## Why Two-Step Submission?

The 404 subnet uses a "king of the hill" competition style where submission timing matters — earlier submissions gain priority. We use a **commit-reveal scheme** with Git's content-addressable hashing:

1. **Commit phase**: Submit only your git commit SHA (a cryptographic hash of your code)
2. **Reveal phase**: Submit your repository reference so validators can fetch and evaluate your code

The block when you call `commit-hash` determines your submission timestamp. Both the hash and repo are revealed on-chain one block after each respective command.

### Why This Works

Git commit hashes are deterministic — they're derived from your code, commit message, author info, and timestamps. You cannot find different code that produces the same hash (SHA-1 collision is computationally infeasible for practical purposes). This means:

- Your hash **commits** you to specific code before anyone sees it
- When you reveal the repo, validators verify the hash matches

### Recommended Workflow
```bash
# 1. Create your solution in a PRIVATE repository
git add . && git commit -m "My solution"
git log --oneline -1
# → a1b2c3d4 ← this is your commit SHA

# 2. Submit the hash to claim your timestamp
python main.py commit-hash --hash a1b2c3d4

# 3. When ready, make your repo public (or keep it private if validators have access)

# 4. Submit the repo reference
python main.py commit-repo --repo https://github.com/you/your-solution
```

### Alternative Approaches

- **Reveal both early, push later**: You can submit both hash and repo immediately, then push your commit to the repo afterward. As long as the commit SHA matches when validators check, it's valid.
- **Update and resubmit**: You can always submit a new solution, but your timestamp resets to the new submission block.

### What Breaks the Hash

Avoid these after submitting your hash — they create new commits with different SHAs:
- `git commit --amend`
- `git rebase`
- Cherry-picking into a different repo
- Re-committing the same files (different timestamp = different hash)