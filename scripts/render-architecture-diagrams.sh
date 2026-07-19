#!/usr/bin/env bash
# Render Mermaid in ARCHITECTURE.md to docs/diagrams/*.png
# Default: Python + Kroki.io (HTTPS). For local Chromium render: MERMAID_LOCAL=1 ./scripts/render-architecture-diagrams.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "${ROOT}/scripts/render_architecture_png.py" "$@"
