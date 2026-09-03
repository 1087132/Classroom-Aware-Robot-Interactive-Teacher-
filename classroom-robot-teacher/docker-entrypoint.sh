#!/bin/sh
# One-time setup inside the container: fetch the neural voice model into the
# data volume (kept across restarts). The app works without it too - the
# robot-face page then falls back to the browser voice.
set -e

TTS_DIR=/app/data/tts
mkdir -p "$TTS_DIR"

if [ ! -f "$TTS_DIR/kokoro-v1.0.onnx" ]; then
  echo "[entrypoint] Downloading neural voice model (~310MB, one time)..."
  curl -sL -o "$TTS_DIR/kokoro-v1.0.onnx" \
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" \
    || echo "[entrypoint] Voice model download failed - continuing with browser voice."
fi
if [ ! -f "$TTS_DIR/voices-v1.0.bin" ]; then
  curl -sL -o "$TTS_DIR/voices-v1.0.bin" \
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" \
    || true
fi

exec "$@"
