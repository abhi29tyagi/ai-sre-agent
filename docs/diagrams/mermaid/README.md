# Mermaid sources (canonical)

These **`.mmd`** files are the **source of truth** for architecture diagrams. [`ARCHITECTURE.md`](../../../ARCHITECTURE.md) embeds **PNG** images so diagrams stay visible on hosts that do not render Mermaid.

| File | PNG output | Topic |
| --- | --- | --- |
| [`01-target-pipeline.mmd`](01-target-pipeline.mmd) | `../architecture-fig1.png` | Target pipeline |
| [`02-figure1-roadmap.mmd`](02-figure1-roadmap.mmd) | `../architecture-fig2.png` | Figure 1 roadmap |
| [`03-figure2-end-to-end.mmd`](03-figure2-end-to-end.mmd) | `../architecture-fig3.png` | Figure 2 end-to-end |
| [`04-figure3-component-map.mmd`](04-figure3-component-map.mmd) | `../architecture-fig4.png` | Figure 3 component map |
| [`05-figure4-internal-pipeline.mmd`](05-figure4-internal-pipeline.mmd) | `../architecture-fig5.png` | Figure 4 internal pipeline |
| [`06-figure5-phase1-sequence.mmd`](06-figure5-phase1-sequence.mmd) | `../architecture-fig6.png` | Figure 5 Phase 1 sequence |
| [`07-figure6-phase2-pr-sequence.mmd`](07-figure6-phase2-pr-sequence.mmd) | `../architecture-fig7.png` | Figure 6 Phase 2 PR |
| [`08-figure7-phase3.mmd`](08-figure7-phase3.mmd) | `../architecture-fig8.png` | Figure 7 Phase 3 |
| [`09-figure8-alert-driven.mmd`](09-figure8-alert-driven.mmd) | `../architecture-fig9.png` | Figure 8 alert-driven |

After editing any `.mmd`, regenerate PNGs from the repo root:

```bash
./scripts/render-architecture-diagrams.sh
```

Preview Mermaid locally with [Mermaid Live Editor](https://mermaid.live) by pasting file contents.
