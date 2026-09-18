# Third-party fixtures this repository does not have

Three golden tests compare against numbers published by someone else. They
are the only tests in the suite whose value comes from outside the engine,
and they are skipped until the files below exist. Nothing is faked in their
place: a fabricated fixture would turn a test that proves something into a
test that proves the generator agrees with itself.

Drop the files in and the tests run. No code changes.

| File | Feeds | What it needs to contain |
| --- | --- | --- |
| `sr1_final_settlements.csv` | PRD-001 AC-5.1 | At least six expired SR1 contracts: `symbol,contract_month,settlement_price,settlement_date` |
| `sr3_final_settlements.csv` | PRD-001 AC-5.2 | At least six expired SR3 contracts, same columns |
| `sofr_fixings.csv` | AC-5.1, AC-5.2 | Published SOFR over the same span: `date,value`, value a decimal (`0.0531`) |
| `whitepaper_2025_strip.csv` | AC-6.6, AC-8.1, AC-8.2 | The SR3 strip the CME/Rogerson 2025 whitepaper lists: `label,start,end,price`, plus `as_of` and `sigma` in the sidecar |

## Why they are missing

The session that built this package had egress limited to package registries
and GitHub by organisation policy. `fred.stlouisfed.org` returned a 403 at the
proxy and CME was unreachable, so neither the fixings nor the settlements nor
the whitepaper could be fetched. This is the escalation that
`docs/forge/plan/001-v1-plan-design.md` task 0.4 exists to surface, arriving
where it was meant to: before the code was built around it, not after.

## What was verified instead

The settlement arithmetic is checked against an independent re-implementation
inside `tests/test_futures_settlement.py`, which catches a wrong formula but
cannot catch a wrong *convention* — that is exactly what the CME comparison is
for, and why it is skipped rather than quietly replaced.

## Whitepaper sidecar

`whitepaper_2025_strip.provenance.json` must record the reconstruction
hypotheses the whitepaper does not state: valuation date, the convexity sigma
and model used to adjust the strip, and the stub convention. PRD-001 decision
A1 requires those to be written down with the fixture, and the tolerance
(±2 contracts, ±0.5 bp, ±3%) assumes they are right. If the strip turns out to
be only partially published, the tolerance does not move — the test becomes a
documented `xfail`.
