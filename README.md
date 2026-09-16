# rates_engine

Deterministic SOFR rates engine: curve construction (discount, zero, par, forward),
SOFR futures with convexity adjustment, OIS/IRS/FRA pricing under collateral discounting,
and swap hedging with futures strips. Every result returns the number **and** the
evidence it rests on. No AI in runtime.

Distribution: `finport-ratesengine` (planned). Import: `rates_engine`. CLI: `rateng`.

Conventions mirror [`alanvaa06/optimization_engine`](https://github.com/alanvaa06/optimization_engine)
(`finport-optengine`).

## Status

Design and PRDs only. No code yet. PRD-001 open questions closed 2026-09-16
(golden curve reconstructed from the CME whitepaper; Treasury par proxy for the long
end; dual-curve solver validated on synthetic inputs; Python >= 3.11).

| Document | Path |
| --- | --- |
| Architecture (phase 1) | `docs/design/2026-09-16-rates-engine-design.md` |
| PRD-001 v1: curves, futures, swaps, hedging | `docs/forge/prd/001-v1-curves-futures-swaps.md` |
| PRD-002 v2: swaptions, caps/floors, vol cube, curve models, MCP | `docs/forge/prd/002-v2-swaptions-vol-mcp.md` |
| PRD-003 v3: MXN curve, FX forwards, FX options, hedge comparator | `docs/forge/prd/003-v3-fx-mxn-hedging.md` |

Documents are in Spanish; paper titles in English. `[[wikilinks]]` point to the
author's private knowledge base (Obsidian vault) and do not resolve here.

## Roadmap

1. **v0.1** (PRD-001): bootstrap OIS-SOFR from fixings, SR1/SR3 futures and an opt-in
   Treasury par proxy for the long end;
   four curve views; Ho-Lee / Hull-White convexity; OIS, IRS, FRA pricing; DV01;
   strip hedge with shock table. Golden test: CME/Rogerson 2025 whitepaper
   (2y OIS 100M -> 779 SR3 contracts, +22,292 USD at -100 bp).
2. **v0.2** (PRD-002): Black / Bachelier swaptions, caps/floors, SABR vol cube,
   Nelson-Siegel / FOMC step curve, monotone-convex interpolation, MCP server.
3. **v0.3** (PRD-003): TIIE curve, USD/MXN forwards with cross-currency basis,
   Garman-Kohlhagen + vanna-volga, CFA L3 hedge-structure comparator.

## License

MIT (to be added with the first code release).
