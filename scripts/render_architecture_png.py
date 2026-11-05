#!/usr/bin/env python3
"""Render Mermaid `.mmd` sources to PNG files for Markdown hosts that do not render Mermaid.

Default inputs: sorted `docs/diagrams/mermaid/*.mmd` → `docs/diagrams/architecture-figN.png`.

Fallback: if no `.mmd` files exist, extract ```mermaid blocks from `--markdown-fallback`
(usually ARCHITECTURE.md) for one-off recovery.

Backends (MERMAID_BACKEND or --backend):
  auto          — per diagram: try docker, npx, kroki, mermaid_ink until one succeeds
  docker        — minlag/mermaid-cli image (diagrams stay local)
  npx           — @mermaid-js/mermaid-cli (Node + Chromium)
  kroki         — POST https://kroki.io/mermaid/png (needs network)
  mermaid_ink   — GET mermaid.ink compressed URL (needs network; long diagrams may 503)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MERMAID_DIR = ROOT / "docs" / "diagrams" / "mermaid"
DEFAULT_OUT = ROOT / "docs" / "diagrams"
DEFAULT_MD_FALLBACK = ROOT / "ARCHITECTURE.md"
# Transient copies when rendering from Markdown-only fallback
TMP_DIR = Path(os.environ.get("MERMAID_TMP_DIR", str(ROOT / ".mermaid")))
KROKI_URL = "https://kroki.io/mermaid/png"
MERMAID_INK_BASE = "https://mermaid.ink/img"
DOCKER_IMAGE = os.environ.get("MERMAID_DOCKER_IMAGE", "minlag/mermaid-cli:11.12.0")
CHROME_UA = "Mozilla/5.0 (compatible; ai-sre-agent-diagrams/1.0)"


def extract_blocks(md_text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", md_text, flags=re.DOTALL)


def mermaid_ink_png_url(diagram: str) -> str:
    graph = {"code": diagram.strip(), "mermaid": {"theme": "default"}}
    data = json.dumps(graph, separators=(",", ":")).encode("utf-8")
    c = zlib.compressobj(9, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)
    compressed = c.compress(data) + c.flush()
    b64 = base64.b64encode(compressed).decode("ascii").replace("+", "-").replace("/", "_")
    return f"{MERMAID_INK_BASE}/pako:{b64}?type=png"


def http_get_png(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": CHROME_UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = resp.read()
        if len(data) < 200 and data[:1] == b"<":
            raise RuntimeError("unexpected HTML response")
        return data


def render_kroki(body: str) -> bytes:
    req = urllib.request.Request(
        KROKI_URL,
        data=body.strip().encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": CHROME_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Kroki HTTP {resp.status}")
        return resp.read()


def docker_available() -> bool:
    return Path("/var/run/docker.sock").exists() and shutil.which("docker") is not None


def render_docker(mmd_path: Path, png_path: Path, root: Path) -> None:
    mmd_rel = mmd_path.resolve().relative_to(root.resolve()).as_posix()
    png_rel = png_path.resolve().relative_to(root.resolve()).as_posix()
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{root}:/data",
        DOCKER_IMAGE,
        "-i",
        f"/data/{mmd_rel}",
        "-o",
        f"/data/{png_rel}",
        "-b",
        "white",
    ]
    subprocess.run(cmd, check=True, cwd=str(root))


def render_npx(mmd_path: Path, png_path: Path, root: Path) -> None:
    cmd = [
        "npx",
        "--yes",
        "@mermaid-js/mermaid-cli@11",
        "-i",
        str(mmd_path),
        "-o",
        str(png_path),
        "-b",
        "white",
    ]
    subprocess.run(cmd, check=True, cwd=str(root))


def backends_auto() -> list[str]:
    out: list[str] = []
    if docker_available():
        out.append("docker")
    if shutil.which("npx"):
        out.append("npx")
    out.extend(["kroki", "mermaid_ink"])
    return out


def render_one(
    body: str,
    png_path: Path,
    root: Path,
    backend: str,
    *,
    canonical_mmd: Path | None,
    tmp_mmd: Path,
) -> None:
    """Use canonical_mmd on disk for docker/npx when present; else write body to tmp_mmd."""
    if backend == "docker":
        mmd = canonical_mmd if canonical_mmd and canonical_mmd.is_file() else tmp_mmd
        if mmd == tmp_mmd:
            tmp_mmd.parent.mkdir(parents=True, exist_ok=True)
            tmp_mmd.write_text(body.strip() + "\n", encoding="utf-8")
        render_docker(mmd, png_path, root)
    elif backend == "npx":
        mmd = canonical_mmd if canonical_mmd and canonical_mmd.is_file() else tmp_mmd
        if mmd == tmp_mmd:
            tmp_mmd.parent.mkdir(parents=True, exist_ok=True)
            tmp_mmd.write_text(body.strip() + "\n", encoding="utf-8")
        render_npx(mmd, png_path, root)
    elif backend == "kroki":
        png_path.write_bytes(render_kroki(body))
    elif backend == "mermaid_ink":
        url = mermaid_ink_png_url(body)
        png_path.write_bytes(http_get_png(url))
    else:
        raise ValueError(f"unknown backend: {backend}")


def load_diagrams(mermaid_dir: Path, markdown_fallback: Path | None) -> list[tuple[str, Path | None]]:
    """Return list of (mermaid_body, optional_canonical_path)."""
    if mermaid_dir.is_dir():
        sources = sorted(mermaid_dir.glob("*.mmd"))
        if sources:
            return [(p.read_text(encoding="utf-8"), p) for p in sources]

    md = markdown_fallback or DEFAULT_MD_FALLBACK
    if not md.is_file():
        return []
    blocks = extract_blocks(md.read_text(encoding="utf-8"))
    return [(b.strip() + "\n", None) for b in blocks]


def main() -> int:
    p = argparse.ArgumentParser(
        description="Render docs/diagrams/mermaid/*.mmd to docs/diagrams/architecture-figN.png",
    )
    p.add_argument(
        "--mermaid-dir",
        type=Path,
        default=Path(os.environ.get("MERMAID_SOURCE_DIR", str(DEFAULT_MERMAID_DIR))),
    )
    p.add_argument(
        "--markdown-fallback",
        type=Path,
        default=None,
        help=f"If mermaid-dir has no .mmd files, extract from this Markdown (default: {DEFAULT_MD_FALLBACK})",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--backend",
        choices=("auto", "docker", "npx", "mermaid_ink", "kroki"),
        default=os.environ.get("MERMAID_BACKEND", "auto"),
    )
    args = p.parse_args()

    diagrams = load_diagrams(args.mermaid_dir, args.markdown_fallback)
    if not diagrams:
        print(
            f"No diagrams: add .mmd files under {args.mermaid_dir} or pass --markdown-fallback",
            file=sys.stderr,
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR
    tmp.mkdir(parents=True, exist_ok=True)

    if args.backend == "auto":
        chain = backends_auto()
        print(f"using backend chain (auto): {' -> '.join(chain)}", file=sys.stderr)
    else:
        chain = [args.backend]
        print(f"using backend: {args.backend}", file=sys.stderr)

    for i, (body, canonical) in enumerate(diagrams, start=1):
        out_png = args.out / f"architecture-fig{i}.png"
        tmp_mmd = tmp / f"architecture-fig{i}.mmd"
        print(f"{i}/{len(diagrams)} -> {out_png.name}", file=sys.stderr)
        ok = False
        last_err: Exception | None = None
        for b in chain:
            try:
                render_one(body, out_png, ROOT, b, canonical_mmd=canonical, tmp_mmd=tmp_mmd)
                ok = True
                if b != chain[0]:
                    print(f"  (ok via fallback: {b})", file=sys.stderr)
                break
            except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, subprocess.CalledProcessError, OSError, FileNotFoundError, ValueError) as e:
                last_err = e
                print(f"  {b} failed: {e}", file=sys.stderr)
                continue
        if not ok:
            print(f"failed fig{i} after trying: {chain} ({last_err})", file=sys.stderr)
            return 1

    print(f"wrote {len(diagrams)} PNG(s) under {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
