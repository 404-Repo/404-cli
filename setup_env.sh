#!/bin/bash

# Stop the script on any error
set -e

# Attempt to find Conda's base directory and source it (required for `conda activate`)
CONDA_BASE=$(conda info --base)

if [ -z "${CONDA_BASE}" ]; then
    echo "Conda is not installed or not in the PATH"
    exit 1
fi

PATH="${CONDA_BASE}/bin/":$PATH
source "${CONDA_BASE}/etc/profile.d/conda.sh"

# Create conda environment and activate it
conda env create -f conda_env.yml
conda activate three-gen-discord-bot
conda info --env

CUDA_HOME=${CONDA_PREFIX}
# TODO: add to the requirements.txt later and not build from sources
pip install git+https://github.com/nerfstudio-project/gsplat.git@v1.4.0

# Store the path of the Conda interpreter
CONDA_INTERPRETER_PATH=$(which python)

# Generate the validation.config.js file for PM2 with specified configurations
cat <<EOF > discord-bot.config.js
module.exports = {
  apps : [{
    name: 'discord-bot',
    script: 'main.py',
    interpreter: '${CONDA_INTERPRETER_PATH}',
  }]
};
EOF

echo -e "\n\n[INFO] discord-bot.config.js generated for PM2."
