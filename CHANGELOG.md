# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A change to a golden test's tolerance is a change to what this package
claims, so it belongs here too.

## [Unreleased]

## [0.1.0] - 2026-09-16

First release. Curves, futures convexity, linear pricing, risk and hedging,
each result carrying the evidence it rests on.

### Added

- **Conventions.** `DayCount` (ACT/360, ACT/365F, 30/360), the SIFMA US
  calendar with the twelve recommended holidays, three roll conventions, IMM
  dates and a single backward-generating `Schedule`.
- **Evidence.** `Evidence` composes rather than flattens: `sources` holds the
  evidence of the inputs, `qualities` is the recursive union, and
  `worst_quality` ranks `observed < synthetic < proxy < assumed`. A hedge
  built on a proxied curve reports the proxy without the hedging code knowing
  what a proxy is.
- **Market data.** `MarketSnapshot` with per-series provenance, SOFR
  compounding and averaging that report how many days reused a previous
  published rate, a `file` provider and a `fred` provider that imports
  `urllib` inside the call.
- **Instruments.** `OISSwap`, `IRSwap` (Term SOFR projection, OIS
  discounting), `FRA`, `SOFRFuture1M` and `SOFRFuture3M`. Contract DV01 is
  derived from notional and nominal tenor: USD 25.00 for SR3, USD 41.67 for
  SR1.
- **Curves.** Log-linear-in-discount-factor interpolation, a sequential
  bootstrap where every instrument exposes one node and one residual, the four
  views with their conventions attached, and a dual-curve solver in both
  sequential and simultaneous modes.
- **Convexity.** Ho-Lee and Hull-White adjustments, with Hull-White converging
  to Ho-Lee as mean reversion vanishes, and sigma either explicit or estimated
  from realised SOFR.
- **Pricing.** `pv`, `par_rate`, `annuity` and a central-difference parallel
  `dv01`.
- **Risk.** `key_rate_dv01` built from tent shocks that form a partition of
  unity, `key_rate_duration`, `pvbp`, `money_duration`, `money_convexity`,
  `effective_duration` and `effective_convexity`.
- **Hedging.** `strip_hedge` sizing contracts from bucketed deltas by
  instrument quote, and `shock_table` reporting the convexity the strip cannot
  remove in dollars and in basis points.
- **CLI.** `rateng bootstrap / price / hedge / describe`, all with `--json`.
- **Docs.** `AGENTS.md`, `llms.txt`, `docs/ERRORS.md`, `docs/RESEARCH.md`,
  `docs/RELEASING.md`.

### Refusals

Seventeen named exceptions, each with an exit code and an entry in
`docs/ERRORS.md`. A missing fixing is never interpolated, an unimplemented
convention never falls back to a default, a Treasury proxy never enters a
curve undeclared, a key rate is never extrapolated, and a duration is never
divided by a zero price.

### Known limits

- The CME whitepaper goldens (AC-6.6, AC-8.1, AC-8.2) and the CME settlement
  comparisons (AC-5.1, AC-5.2) are **skipped**: the build environment's egress
  policy blocked both CME and FRED, so the third-party fixtures could not be
  fetched. Tolerances are unchanged and the tests activate as soon as the
  files land in `tests/fixtures/cme_published/`. Nothing was substituted in
  their place.
- The SIFMA Saturday-observance rule is implemented but **not yet checked**
  against SIFMA's published calendar. The fixture's provenance records this.
- The dual-curve solver is validated against synthetic inputs only; there is
  no free source of Term SOFR par or basis quotes. Every such result declares
  `inputs_origin="synthetic"`.
- The SR1 contract notional of USD 5,000,000 is assumed rather than read from
  the contract spec.
- The Hull-White convexity formula is transcribed via Skov and Skovmand rather
  than read in Henrard 2018. The Ho-Lee limit test is what guards it.

[Unreleased]: https://github.com/alanvaa06/rates_engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/alanvaa06/rates_engine/releases/tag/v0.1.0
