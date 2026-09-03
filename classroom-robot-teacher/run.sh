#!/bin/bash
# Start the Classroom-Aware Interactive Robot Teacher (CAT) prototype.
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

# Start the local offline LLM server (Ollama) if installed and not running
if command -v ollama >/dev/null 2>&1 && ! curl -s --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "  Starting local LLM server (ollama serve)..."
  nohup ollama serve > /tmp/ollama.log 2>&1 &
  sleep 1
fi

echo ""
echo "  Starting CAT - Classroom-Aware Interactive Robot Teacher"
echo "  Teacher dashboard:  http://localhost:8000/teacher"
echo "  Students join at:   http://$(ipconfig getifaddr en0 2>/dev/null || echo localhost):8000/student"
echo ""
exec .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
