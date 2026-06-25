#!/usr/bin/env bash
# Pull base models and create job-answers / job-verify aliases with baked-in system prompts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v ollama >/dev/null 2>&1; then
  echo "Install Ollama: brew install ollama && brew services start ollama"
  exit 1
fi

# Read llm.verifier_enabled from config.yaml (fallback config.example.yaml).
# Defaults to enabled when no config / parser is available.
CONFIG_FILE="config.yaml"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="config.example.yaml"

verifier_enabled() {
  [ -f "$CONFIG_FILE" ] || return 0
  python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || return 0
import sys
try:
    import yaml
except Exception:
    sys.exit(0)  # no yaml -> default enabled
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f) or {}
except Exception:
    sys.exit(0)
llm = data.get("llm", {}) or {}
sys.exit(0 if llm.get("verifier_enabled", True) else 1)
PY
}

if verifier_enabled; then
  VERIFIER=1
else
  VERIFIER=0
fi

echo "=== Pull base models ==="
ollama pull qwen2.5:7b
if [ "$VERIFIER" -eq 1 ]; then
  ollama pull qwen2.5:3b
else
  echo "Skipping qwen2.5:3b — llm.verifier_enabled is false in $CONFIG_FILE"
fi

echo ""
echo "=== Create job-answers (generator) ==="
ollama create job-answers -f ollama/Modelfile.job-answers

if [ "$VERIFIER" -eq 1 ]; then
  echo ""
  echo "=== Create job-verify (high-risk verifier) ==="
  ollama create job-verify -f ollama/Modelfile.job-verify
else
  echo ""
  echo "=== Skipping job-verify — llm.verifier_enabled is false in $CONFIG_FILE ==="
fi

echo ""
echo "Done. config.yaml should use:"
echo "  llm.model: job-answers"
if [ "$VERIFIER" -eq 1 ]; then
  echo "  llm.verifier_model: job-verify"
fi
echo ""
echo "M2 16GB tip — limit Ollama concurrent requests (add to ~/.zshrc or launchd):"
echo "  export OLLAMA_NUM_PARALLEL=1"
echo "  export OLLAMA_MAX_LOADED_MODELS=2"
echo ""
echo "Test:"
echo '  ollama run job-answers "Profile: notice 0 days. Question: notice period? Return JSON."'
