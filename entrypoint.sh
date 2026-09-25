#!/bin/bash
set -e

if [ -z "$GH_URL" ] || [ -z "$GH_TOKEN" ]; then
  echo "Error: GH_URL and GH_TOKEN must be provided."
  exit 1
fi

NAME="${RUNNER_NAME:-docker-runner-$(hostname)}"
LABELS="${GH_LABELS:-gpu-llm-node}"

echo "Configuring GitHub Runner: $NAME for $GH_URL..."

./config.sh --url "${GH_URL}" \
            --token "${GH_TOKEN}" \
            --name "${NAME}" \
            --labels "${LABELS}" \
            --unattended \
            --replace

cleanup() {
    echo "Deregistering runner from GitHub..."
    ./config.sh remove --token "${GH_TOKEN}" || true
}

trap 'cleanup' INT TERM EXIT

exec ./run.sh
