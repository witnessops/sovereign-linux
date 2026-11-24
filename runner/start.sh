#!/usr/bin/env bash
# Run as core in the guest. Supply one disposable broker token on stdin.
set -euo pipefail
IFS= read -r N8N_RUNNERS_AUTH_TOKEN
: "${N8N_RUNNERS_AUTH_TOKEN:?missing broker token}"
export N8N_RUNNERS_AUTH_TOKEN
image=8045f3fa0f22bb5e140133c95bebed0f7293867a0b3a47a1dc5453b0d61ea689
[[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]]
echo '4f4219297165cbd9e47d2219c1a4aa5196b9257ef99e5eb693fc7818967e34f0  /var/mnt/runner/runner.tar' | sha256sum -c -
podman load --input /var/mnt/runner/runner.tar
[[ $(podman image inspect --format '{{.Id}}' "$image") == "$image" ]]
exec podman run -d --pull=never --name sovereign-n8n-runner \
    --network=host --user=65532:65532 --read-only --read-only-tmpfs=false \
    --cap-drop=all --security-opt=no-new-privileges --pids-limit=128 \
    --memory=768m --cpus=1 --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m \
    --env N8N_RUNNERS_AUTH_TOKEN --env N8N_RUNNERS_TASK_BROKER_URI=http://127.0.0.1:5679 \
    --env N8N_RUNNERS_MAX_CONCURRENCY=1 --env N8N_RUNNERS_TASK_TIMEOUT=60 \
    --env N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT=15 "$image"
