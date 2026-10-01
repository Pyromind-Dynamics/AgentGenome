#!/usr/bin/env bash
# AgentGenome remains a plugin; SDK owns the server and execution environment.
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

AGENTGENOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AGENTGENOME_DIR
mode="${1:-test}"
if [[ $# -gt 0 ]]; then shift; fi
case "${mode}" in
  -h|--help|help)
    cat <<'HELP'
Usage: ./start_sdk.sh [test|test-revision|start]
  test   Run two CSV batches in SDK's Pi OS sandbox, then publish data-cleaning.
         No model key, DataFlow runtime or business platform is needed.
  test-revision  Validate legacy CSV revision, regression cases and automatic latest publication.
  start  Start the SDK server with the AgentGenome plugin enabled.

Environment:
  SOFTWARE_AGENT_SDK_DIR   SDK checkout (default: ../software-agent-sdk)
  AGENTGENOME_HOME         Shared asset registry (same default as SDK startup)
  AGENTGENOME_SKIP_INSTALL=1  Test only: reuse installed Python/npm dependencies

Stop the SDK using this registry before running test. This does not start an
independent AgentGenome service. For source changes, rebuild SDK's pinned
wheel/npm package first; see docs/sdk-v1.md.
HELP
    exit 0 ;;
  test|test-revision|start) ;;
  *) echo "Unknown mode: ${mode}; use --help." >&2; exit 2 ;;
esac
if [[ $# -ne 0 ]]; then
  echo "Unexpected arguments: $*; configure paths using environment variables." >&2
  exit 2
fi
export SOFTWARE_AGENT_SDK_DIR="${SOFTWARE_AGENT_SDK_DIR:-${AGENTGENOME_DIR}/../software-agent-sdk}"
if [[ ! -f "${SOFTWARE_AGENT_SDK_DIR}/start_inference.sh" ]]; then
  echo "SDK not found: ${SOFTWARE_AGENT_SDK_DIR}; set SOFTWARE_AGENT_SDK_DIR." >&2
  exit 1
fi
SOFTWARE_AGENT_SDK_DIR="$(cd "${SOFTWARE_AGENT_SDK_DIR}" && pwd)"
cd "${SOFTWARE_AGENT_SDK_DIR}"
export workspace_dir="${workspace_dir:-${WORKSPACE_DIR:-${SOFTWARE_AGENT_SDK_DIR}/workspace}}"
export WORKSPACE_DIR="${workspace_dir}"
export OH_CONVERSATIONS_PATH="${OH_CONVERSATIONS_PATH:-${WORKSPACE_DIR}/conversations}"
export AGENTGENOME_HOME="${AGENTGENOME_HOME:-$(dirname "${OH_CONVERSATIONS_PATH}")/agentgenome}"
export PYROMIND_AGENTGENOME_ENABLED=1

if [[ "${mode}" == "start" ]]; then
  exec bash "${SOFTWARE_AGENT_SDK_DIR}/start_inference.sh" --agentgenome
fi

if [[ -x "${HOME}/.local/bin/uv" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi
for dependency in uv npm; do
  command -v "${dependency}" >/dev/null 2>&1 || {
    echo "Required command not found: ${dependency}" >&2; exit 127;
  }
done
case "$(uname -s)" in
  Darwin)
    [[ -x /usr/bin/sandbox-exec ]] || { echo "Missing /usr/bin/sandbox-exec" >&2; exit 1; } ;;
  Linux)
    for dependency in rg bwrap socat; do
      command -v "${dependency}" >/dev/null 2>&1 || {
        echo "OS sandbox requires ${dependency}." >&2; exit 1;
      }
    done ;;
  *) echo "OS sandbox testing supports Linux and macOS." >&2; exit 1 ;;
esac
pi_runtime="${SOFTWARE_AGENT_SDK_DIR}/harness-adapter/pi-runtime"
if [[ "${AGENTGENOME_SKIP_INSTALL:-0}" != "1" ]]; then
  uv sync --frozen
  npm --prefix "${pi_runtime}" ci
elif [[ ! -x .venv/bin/python || ! -d "${pi_runtime}/node_modules" ]]; then
  echo "Dependencies missing; run test without AGENTGENOME_SKIP_INSTALL=1 first." >&2
  exit 1
fi
npm --prefix "${pi_runtime}" run build
export PYROMIND_PI_RUNTIME="${pi_runtime}/dist/index.js"
echo "Validating with SDK Pi os-sandbox (no model calls)."
echo "Shared assets: ${AGENTGENOME_HOME}"
echo "Stop any SDK instance using this registry before continuing."
template="${AGENTGENOME_DIR}/templates/data-cleaning"
validation_args=()
if [[ "${mode}" == "test-revision" ]]; then
  template="${AGENTGENOME_DIR}/templates/data-cleaning"
  validation_args+=(--test-revisions)
fi
uv run --no-sync python scripts/validate_agentgenome.py \
  --template "${template}" \
  --home "${AGENTGENOME_HOME}" \
  --workspace "${WORKSPACE_DIR}/genome-validation" "${validation_args[@]}"
echo "Validation passed; the validated data-cleaning version is published."
echo "Next: ${AGENTGENOME_DIR}/start_sdk.sh start"
