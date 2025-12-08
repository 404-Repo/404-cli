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

### Commit hash of the your solution
```bash
python main.py commit-hash --hash <your_commit_sha>
```

### Commit repository reference of your solution
```bash
python main.py commit-repo --repo <reference_to_your_repo>
```
