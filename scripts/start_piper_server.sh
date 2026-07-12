#!/usr/bin/env bash
set -euo pipefail

VOICE="${PIPER_VOICE:-en_US-lessac-medium}"
HOST="${PIPER_HOST:-127.0.0.1}"
PORT="${PIPER_PORT:-5001}"

if [[ ! -f "${VOICE}.onnx" || ! -f "${VOICE}.onnx.json" ]]; then
  python3 -m piper.download_voices "${VOICE}"
fi

exec python3 -m piper.http_server \
  -m "${VOICE}" \
  --host "${HOST}" \
  --port "${PORT}"
