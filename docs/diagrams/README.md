# Architecture diagram assets

Bitbucket does **not** render Mermaid in Markdown, so [`ARCHITECTURE.md`](../../ARCHITECTURE.md) uses **PNG** images.

## Layout

| Path | Purpose |
| --- | --- |
| [`mermaid/`](mermaid/) | **Canonical** `.mmd` sources (version these; edit here first). |
| `architecture-fig1.png` … `architecture-fig9.png` | Generated bitmaps referenced from `ARCHITECTURE.md`. |

Mapping from filename order (sorted `mermaid/*.mmd`) to PNG index is in [`mermaid/README.md`](mermaid/README.md).

## Regenerate PNGs

From the repo root:

```bash
./scripts/render-architecture-diagrams.sh
```

This runs [`../scripts/render_architecture_png.py`](../scripts/render_architecture_png.py), which reads **`docs/diagrams/mermaid/*.mmd`** in sorted order. If that directory is empty, it falls back to extracting fenced Mermaid from `ARCHITECTURE.md` (for recovery only).

### Backend selection

See [`mermaid/README.md`](mermaid/README.md) (Kroki, mermaid.ink, Docker, `npx`, privacy).

The render script writes short-lived files under **`.mermaid/`** in the repo root (gitignored). Override with `MERMAID_TMP_DIR` if needed.
