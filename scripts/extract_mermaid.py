#!/usr/bin/env python3
"""Extract ```mermaid ... ``` blocks from a Markdown file into separate .mmd files.

Example (bootstrap or recovery):

  python3 scripts/extract_mermaid.py ARCHITECTURE.md docs/diagrams/mermaid/

Prefer maintaining sources under docs/diagrams/mermaid/ and embedding PNG in docs.
"""
import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_mermaid.py <input.md> <output_dir>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)
    if not blocks:
        print("no mermaid blocks found", file=sys.stderr)
        return 1
    for i, body in enumerate(blocks, start=1):
        path = out_dir / f"architecture-fig{i}.mmd"
        path.write_text(body.strip() + "\n", encoding="utf-8")
        print(path)
    print(f"extracted {len(blocks)} block(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
