#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
container=${ZEROCLAW_CONTAINER:-zeroclaw}
config_path=${ZEROCLAW_CONFIG:-/zeroclaw-data/.zeroclaw/config.toml}
if ! command docker info >/dev/null 2>&1; then
  sudo -v
  docker() { sudo docker "$@"; }
fi
knowledge_container=${KNOWLEDGE_CONTAINER:-}
if [[ -z "$knowledge_container" ]]; then
  if docker inspect personal-knowledge >/dev/null 2>&1; then
    knowledge_container=personal-knowledge
  else
    knowledge_container=$(docker compose ps -q knowledge)
  fi
fi
if [[ -z "$knowledge_container" ]]; then
  echo 'Deploy the knowledge stack first, or set KNOWLEDGE_CONTAINER to its container name.'
  exit 1
fi
image=$(docker inspect --format '{{.Config.Image}}' "$knowledge_container")
docker exec "$container" zeroclaw --version
docker exec "$container" zeroclaw status --format=exit-code
docker exec "$knowledge_container" python smoke_mcp.py
scratch=$(mktemp -d /tmp/zeroclaw-knowledge.XXXXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
docker exec -u 0 "$container" cat "$config_path" > "$scratch/config.original.toml"
cp ZEROCLAW-INSTRUCTIONS.md "$scratch/instructions.md"
docker run --rm --network none --user 1000:1000 -v "$scratch:/integration" "$image" \
  python integration.py --config-path "$config_path" \
  --agent "${ZEROCLAW_AGENT:-}" --workspace "${ZEROCLAW_WORKSPACE:-}"
workspace=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["workspace"])' "$scratch/plan.json")
if ! docker exec -u 0 "$container" test -d "$workspace"; then
  echo "Existing workspace not found: $workspace"
  echo 'Set ZEROCLAW_WORKSPACE to your existing agent workspace and rerun. Config has not been changed.'
  exit 1
fi
agents_file="$workspace/AGENTS.md"
had_agents=0
if docker exec -u 0 "$container" test -f "$agents_file"; then
  had_agents=1
  docker exec -u 0 "$container" cat "$agents_file" > "$scratch/AGENTS.original.md"
else
  : > "$scratch/AGENTS.original.md"
fi
docker run --rm --network none --user 1000:1000 -v "$scratch:/integration" "$image" \
  python integration.py --config-path "$config_path" --instructions

if [[ $(docker inspect --format '{{if index .NetworkSettings.Networks "zeroclaw-knowledge"}}yes{{end}}' "$container") != yes ]]; then
  docker network connect zeroclaw-knowledge "$container"
fi
# Test MCP from the *same network namespace* used by the running ZeroClaw bot.
docker run --rm --network "container:$container" --entrypoint python "$image" /app/smoke_mcp.py

stamp=$(date +%Y%m%d-%H%M%S)
backup="$config_path.before-knowledge-$stamp"
agents_backup="$agents_file.before-knowledge-$stamp"
docker exec -u 0 "$container" cp -p "$config_path" "$backup"
if [[ "$had_agents" == 1 ]]; then
  docker exec -u 0 "$container" cp -p "$agents_file" "$agents_backup"
fi
rollback() {
  echo 'Integration failed; restoring the previous ZeroClaw files.'
  docker exec -u 0 "$container" cp -p "$backup" "$config_path" || true
  if [[ "$had_agents" == 1 ]]; then
    docker exec -u 0 "$container" cp -p "$agents_backup" "$agents_file" || true
  else
    docker exec -u 0 "$container" rm -f "$agents_file" || true
  fi
  docker restart "$container" >/dev/null || true
}
trap 'rollback; exit 1' ERR
# Replace atomically while preserving the running container's file ownership/mode.
docker exec -i -u 0 "$container" sh -ec \
  'cat > "$1.knowledge-new"; chown --reference="$1" "$1.knowledge-new"; chmod --reference="$1" "$1.knowledge-new"; mv "$1.knowledge-new" "$1"' \
  sh "$config_path" < "$scratch/config.updated.toml"
docker exec -i -u 0 "$container" sh -ec \
  'cat > "$1.knowledge-new"; if [ -f "$1" ]; then chown --reference="$1" "$1.knowledge-new"; chmod --reference="$1" "$1.knowledge-new"; else chown --reference="$2" "$1.knowledge-new"; chmod 600 "$1.knowledge-new"; fi; mv "$1.knowledge-new" "$1"' \
  sh "$agents_file" "$workspace" < "$scratch/AGENTS.updated.md"
docker restart "$container" >/dev/null
healthy=0
for attempt in {1..20}; do
  if docker exec "$container" zeroclaw status --format=exit-code >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != 1 ]]; then
  rollback
  exit 1
fi
trap - ERR
echo "Updated existing bot. Config backup: $backup"
echo 'Verify in Discord: ask the existing bot to check its knowledge-base status.'
echo 'If the ZeroClaw container is recreated by Dockhand, rerun this connector to reattach its Docker network.'
