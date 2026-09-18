# rates_engine

Deterministic SOFR rates engine: curve construction (discount, zero, par, forward),
SOFR futures with convexity adjustment, OIS/IRS/FRA pricing under collateral
discounting, key-rate and duration risk, swap hedging with futures strips; swaptions, caps and floors with Bachelier
and Black, a SABR volatility cube, Nelson-Siegel and FOMC-step curves,
monotone-convex interpolation and an MCP server; and — since v0.3 — a second
currency, USD/MXN forwards and options, and a hedge-structure comparator.
Every result returns the number **and** the evidence it rests on. No AI in runtime.

Distribution: `finport-ratesengine`. Import: `rates_engine`. CLI: `rateng`.
Python 3.11+. Core dependencies: numpy, pandas, scipy — and nothing else.

Conventions mirror [`alanvaa06/optimization_engine`](https://github.com/alanvaa06/optimization_engine)
(`finport-optengine`).

## Install

```bash
pip install finport-ratesengine            # curves, pricing, risk, hedging, options
pip install "finport-ratesengine[config]"  # + YAML configs for the CLI
pip install "finport-ratesengine[mcp]"     # + the rateng-mcp server
```

Options, the SABR cube and the parametric curves are all core: the extras buy
a config format and a transport, never a calculation.

## Quickstart

```python
from datetime import date, timedelta

from rates_engine import (
    CurveSet, FuturesNode, OISSwap, RealizedStubNode,
    bootstrap_discount_curve, dv01, imm_date, key_rate_dv01,
    next_imm_on_or_after, par_rate,
)
from rates_engine.hedging import shock_table, strip_hedge

as_of = date(2026, 1, 15)
start = imm_date(2026, 3)

# A realised stub, then two years of SR3 quarters quoted at a flat 4%.
instruments = [
    RealizedStubNode(end=start, accrual_factor=1.0 + 0.04 * ((start - as_of).days / 360.0))
]
period = start
for index in range(9):
    following = next_imm_on_or_after(period + timedelta(days=1))
    instruments.append(
        FuturesNode(start=period, end=following, forward_rate=0.04, label=f"SR3-{index + 1}")
    )
    period = following

curve = bootstrap_discount_curve(as_of, tuple(instruments))
curve_set = CurveSet(curve.curve)
print(f"worst residual   {max(abs(r) for r in curve.residuals_bp.values()):.2e} bp")
print(f"data quality     {curve.evidence.worst_quality.value}")

# A two-year payer OIS on 100 million, struck at its own par rate.
swap = OISSwap(effective=start, maturity=period, fixed_rate=0.04, notional=100_000_000.0)
swap = OISSwap(
    effective=start, maturity=period,
    fixed_rate=par_rate(swap, curve_set).value, notional=100_000_000.0,
)
print(f"par rate         {swap.fixed_rate * 100:.4f}%")
print(f"DV01             {dv01(swap, curve_set).value:,.2f} USD/bp")

profile = key_rate_dv01(swap, curve_set, (0.5, 1.0, 1.5, 2.0))
print(f"key rates sum to {profile.total:,.2f} USD/bp")

hedge = strip_hedge(swap, tuple(instruments), as_of=as_of)
print(f"SR3 contracts    {hedge.total_contracts:,.1f}")
print(f"hedge ratio      {hedge.hedge_ratio:.6f}")

worst = shock_table(hedge).table.set_index("shock_bp").loc[-100.0]
print(f"net at -100bp    {worst.net_pnl:,.0f} USD ({worst.net_per_dv01_bp:.2f} bp)")
```

```text
worst residual   5.97e-12 bp
data quality     observed
par rate         4.0539%
DV01             -21,870.88 USD/bp
key rates sum to -21,870.88 USD/bp
SR3 contracts    878.0
hedge ratio      -1.000000
net at -100bp    -31,289 USD (-1.39 bp)
```

That output is verified by `tests/test_readme_quickstart.py`, which runs every
Python block on this page and compares the first one's output line for line.

## The CLI

```bash
rateng describe --json                      # capabilities; needs no config
rateng bootstrap --config config.json --json   # curve plus its four views
rateng price --config config.json --json       # pv, par, annuity, every risk measure
rateng hedge --config config.json --json       # contracts per period plus the shock table
rateng list-instruments --json              # what this build prices, and what each needs
rateng fx-forward --config fx.json --json      # CIP forward, with the basis kept separate
rateng hedge-structures --config fx.json --json  # seven structures, costed side by side
```

`python scripts/write_example_config.py config.json` writes a config to start
from. With `--json`, stdout is exactly one parseable document and all
narration goes to stderr. Exit code 1 means the inputs are unusable, 2 means
the calculation is impossible.

## The MCP server

```bash
pip install "finport-ratesengine[mcp]"
rateng-mcp
```

Five tools, the same five commands. Each one is literally the callable the
CLI dispatches to, so a tool's answer is byte-identical to the corresponding
`--json` document rather than a second serialisation kept in step by hand.
The server is transport: it writes no file, opens no socket and invokes no
model, and `tests/test_mcp_server.py` asserts all three by running the
handlers under guards that make the first two fatal.

Without the SDK the module still imports, and `build_server()` names the
extra that installs it instead of raising `ModuleNotFoundError`.

## What it refuses

A missing fixing is never interpolated. An unimplemented convention never
falls back to a default. A Treasury par yield never enters a curve without
being declared. A key rate is never extrapolated past the curve. A duration is
never divided by a zero price — a par swap is worth nothing, so
`effective_duration` raises and points you at `dv01` and `money_convexity`.
Macaulay and modified duration need a yield, and therefore a bond, so they
raise rather than being approximated by effective duration under their own
names.

A currency never mixes with another: discounting a peso cashflow on a dollar
curve raises rather than returning a number, and there is no implicit
conversion anywhere, because converting needs a rate, a date and a quoting
convention. A delta never names a strike until the convention is stated —
FX has four, and they differ by hundreds of pips. A Mexican convention this
build could not verify is never presented as one it knows: every peso result
names what it assumed and can be made to refuse outright. And the engine
never recommends a hedge: it compares them and reports the trade-off as
labelled axes, with no function anywhere whose name contains `recommend`.

A volatility is never a bare float, and never converted between normal and
lognormal, because there is no such conversion — only an at-the-money
equivalence, named for what it is. A lognormal model is never used on a
non-positive forward. A volatility cube never interpolates across expiry or
tenor: filling a strike inside a quoted smile is a model fitted to data,
filling a missing slice is a model fitted to a different slice. An asymptotic
expansion never returns a negative volatility.

Twenty-nine named exceptions, each with an exit code and an entry in
[`docs/ERRORS.md`](docs/ERRORS.md) saying whether it is recoverable and what
to do about it.

## Evidence

Evidence nests rather than flattens, so a degradation recorded deep in the
chain is still visible at the top:

```python
from datetime import date, timedelta

from rates_engine import (
    CurveSet, FuturesNode, OISSwap, RealizedStubNode,
    bootstrap_discount_curve, imm_date, next_imm_on_or_after,
)
from rates_engine.diagnostics import quality_report
from rates_engine.hedging import strip_hedge

as_of = date(2026, 1, 15)
start = imm_date(2026, 3)
instruments = [
    RealizedStubNode(end=start, accrual_factor=1.0 + 0.04 * ((start - as_of).days / 360.0))
]
period = start
for index in range(9):
    following = next_imm_on_or_after(period + timedelta(days=1))
    instruments.append(
        FuturesNode(start=period, end=following, forward_rate=0.04, label=f"SR3-{index + 1}")
    )
    period = following

curve = bootstrap_discount_curve(as_of, tuple(instruments))
swap = OISSwap(effective=start, maturity=period, fixed_rate=0.0405, notional=100_000_000.0)
hedge = strip_hedge(swap, tuple(instruments), as_of=as_of)

print(hedge.evidence.worst_quality.value)
print(hedge.evidence.sources[0].produced_by)
print(quality_report(hedge.evidence)["depth"], "levels deep")
```

which prints

```text
observed
curves.bootstrap_discount_curve
2 levels deep
```

A curve built on Treasury par yields standing in for OIS par marks every node
it touches, records a degradation naming the swap spread as an unquantified
bias, and carries both into any price or hedge built on it — without the
pricing or hedging code knowing what a proxy is.

## Honest limits

- **The CME whitepaper goldens are skipped.** 779 contracts, a 3.3304% IMM par
  coupon and +22,292 USD at minus a hundred basis points are the only
  third-party numbers this package is measured against, and the build
  environment could not reach the whitepaper or CME's published settlements.
  The tolerances are unchanged and the tests activate the moment the fixtures
  land in `tests/fixtures/cme_published/`. Nothing was faked in their place;
  see that directory's README.
- **The SIFMA Saturday-observance rule has not been checked** against SIFMA's
  published calendar. It is implemented and the fixture's provenance records
  the check as outstanding.
- **The dual-curve solver is validated on synthetic inputs.** No free source
  of Term SOFR par or basis swap quotes exists, so v1 proves the solver rather
  than a market fit, and every such result declares
  `inputs_origin="synthetic"`.
- **The SR1 contract notional is assumed**, not read from the contract spec.
  The evidence says `notional_source="assumed"`.
- **Hagan's SABR expansion and Hagan-West's interpolation are transcribed**
  from secondary sources. What stands in for reading them: the limits each
  formula must satisfy, a Monte Carlo of the SABR process the expansion
  approximates, and — for Hagan-West — the positivity property the paper is
  about, asserted on shapes that go negative with the collar switched off.
- **The v2 fixtures are constructed.** No free source of swaption quotes
  exists. Each is built from a shape the model being fitted cannot reproduce
  — a quadratic smile for SABR, a Svensson curve for Nelson-Siegel, a
  tick-rounded strip for the FOMC path — so the residual measures the model
  and not the solver. Each says so in its provenance sibling.
- **SABR's β is fixed, not calibrated.** With one smile, β and ρ are close to
  unidentifiable. The choice travels in every calibration payload.
- **No MXN convention is verified.** Banxico, ISDA and CME are all unreachable
  from the build environment, and QuantLib — the one substantive source that
  is — has no TIIE index at all. The TIIE day count, the 28-day coupon period,
  the TIIE 28 versus TIIE de Fondeo distinction and the SIE series identifiers
  are assumptions, named in `UNRESOLVED_MXN`, carried as a degradation on
  every peso result, and refusable with `strict_conventions=True`. Resolving
  them shortens a tuple and changes nothing else.
- **The Mexican calendar is the BMV's, not Banxico's.** Different lists, and
  the diff has not been made. See `tests/fixtures/bmv_holidays.csv`.
- **There is no default FX delta convention, and no default ATM convention.**
  Same reason. Asking for a strike without stating one raises.
- **Vanna-volga is not arbitrage free.** Past the quoted pillars the
  volatility is held flat and the reading is marked unreliable rather than
  extrapolated into a negative density.

[`docs/RESEARCH.md`](docs/RESEARCH.md) maps every formula to its source and
says whether that source was read directly.

## Documents

| Document | Path |
| --- | --- |
| Agent notes | `AGENTS.md` |
| Refusal contract | `docs/ERRORS.md` |
| Formula provenance | `docs/RESEARCH.md` |
| Releasing | `docs/RELEASING.md` |
| Architecture (phase 1) | `docs/design/2026-09-16-rates-engine-design.md` |
| PRD-001 v1 | `docs/forge/prd/001-v1-curves-futures-swaps.md` |
| Plan-design v1 | `docs/forge/plan/001-v1-plan-design.md` |
| PRD-002 v2: swaptions, vol cube, MCP | `docs/forge/prd/002-v2-swaptions-vol-mcp.md` |
| PRD-003 v3: MXN, FX forwards and options | `docs/forge/prd/003-v3-fx-mxn-hedging.md` |

Design documents are in Spanish; paper titles in English. `[[wikilinks]]`
point to the author's private knowledge base and do not resolve here.

## Roadmap

1. **v0.1** (PRD-001): OIS-SOFR bootstrap from fixings, SR1/SR3
   futures and an opt-in Treasury par proxy for the long end; four curve
   views; Ho-Lee and Hull-White convexity; OIS, IRS and FRA pricing; DV01,
   key-rate DV01 and the duration conventions; strip hedge with a shock table.
2. **v0.2** (PRD-002): Black and Bachelier swaptions, caps and
   floors, a SABR vol cube, Nelson-Siegel and FOMC-step curves,
   monotone-convex interpolation, an MCP server.
3. **v0.3** (PRD-003, this release): a second currency carried in the type,
   the BMV calendar, a TIIE curve that marks every convention it could not
   verify, USD/MXN forwards with the cross-currency basis kept separate,
   Garman-Kohlhagen with vanna-volga and four explicit delta conventions, a
   CFA Level III hedge-structure comparator, and the hedging policy as data.

Planned for v1.1: `FixedRateBond`, which brings Macaulay and modified
duration with it, and a real quote source for the dual-curve solver.

## License

MIT. See `LICENSE`.
