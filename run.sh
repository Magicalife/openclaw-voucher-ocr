#!/usr/bin/env bash
set -euo pipefail

FOLDER_PATH="${1:-}"
if [[ -z "${FOLDER_PATH}" ]]; then
  echo "Usage: $0 <folder_path>" >&2
  exit 2
fi

SKILLDIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SKILLDIR/artifacts/latest"
mkdir -p "$OUTDIR"

# Keep OpenClaw on a known-good Node path (brew node >= 22).
export PATH="/opt/homebrew/bin:/usr/bin:/bin:$PATH"

# Default to gateway token from config when not explicitly exported.
if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  if command -v jq >/dev/null 2>&1; then
    OPENCLAW_GATEWAY_TOKEN="$(jq -r '.gateway.auth.token // empty' "$HOME/.openclaw/openclaw.json" 2>/dev/null || true)"
    export OPENCLAW_GATEWAY_TOKEN
  fi
fi

/opt/homebrew/bin/python3.13 "$SKILLDIR/scripts/run_with_vision.py" \
  --folder "$FOLDER_PATH" \
  --outdir "$OUTDIR" \
  --skilldir "$SKILLDIR" \
  --vision-timeout-s 30 \
  "${@:2}"

echo "Wrote: $OUTDIR/vouchers.jsonl"
echo "Wrote: $OUTDIR/timings.jsonl"
echo "Wrote: $OUTDIR/l1_results.jsonl"
echo "Wrote: $OUTDIR/paddle_results.jsonl"
echo "Wrote: $OUTDIR/paddle_timings.jsonl"
echo "Wrote: $OUTDIR/cross_validation.jsonl"
echo "Wrote: $OUTDIR/vision_sub2api_gpt-5.2.jsonl"
echo "Wrote: $OUTDIR/vision_timings.jsonl"
echo "Wrote: $OUTDIR/summary.txt"
