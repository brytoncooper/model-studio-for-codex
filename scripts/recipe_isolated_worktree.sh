#!/bin/zsh
set -euo pipefail

source_directory="${0:A:h:h}"
branch_name="${1:-isolated-work}"
worktree_parent="${source_directory}/work/worktrees"
worktree_path="${worktree_parent}/${branch_name}"
artifact_root="${worktree_path}/dist"
state_root="${worktree_path}/state"
socket_root="${worktree_path}/run"
app_output="${artifact_root}/Model Deck.app"

echo "Isolated development recipe (commands are not executed automatically):"
echo "  git worktree add \"${worktree_path}\" -b \"${branch_name}\""
echo "  mkdir -p \"${artifact_root}\" \"${state_root}\" \"${socket_root}\""
echo "  export MODEL_DECK_APP_OUTPUT=\"${app_output}\""
echo "  export MODEL_DECK_STATE_ROOT=\"${state_root}\""
echo "  export MODEL_DECK_ARTIFACT_ROOT=\"${artifact_root}\""
echo "  export MODEL_DECK_SOCKET_ROOT=\"${socket_root}\""
echo "G0 verification example:"
echo "  python3 scripts/verify.py development-guard --state-root \"${state_root}\" --artifact-root \"${artifact_root}\" --socket-root \"${socket_root}\""
echo "Worker editing check example (after finalizer approval):"
echo "  python3 scripts/editing_check.py --files test_development_guard.py"
