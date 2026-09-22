# Notes for coding agents

A map of this library, written for something that has to produce working code
without reading every module first.

Install `finport-ratesengine`; import `rates_engine`; the console script is
`rateng`. The three names differ on purpose, mirroring
`alanvaa06/optimization_engine`.

```bash
pip install finport-ratesengine            # core: curves, pricing, risk, hedging
pip install "finport-ratesengine[config]"  # + YAML configs for the CLI
```

Python 3.11 or newer. Core dependencies are numpy, pandas and scipy, and
nothing else.

Two places to look when this file does not answer the question.
[`docs/ERRORS.md`](docs/ERRORS.md) is the refusal contract: which of the
thirty-three exception types to catch, which are recoverable, the CLI's exit
codes, and the failures that are *reported* rather than raised.
[`docs/RESEARCH.md`](docs/RESEARCH.md) maps every formula to the paper it came
from and says whether that paper was read or taken from a secondary source.

## The shortest correct program

```python
from datetime import date, timedelta

from rates_engine import (
    CurveSet, FuturesNode, OISSwap, RealizedStubNode,
    bootstrap_discount_curve, dv01, imm_date, next_imm_on_or_after, par_rate,
)

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
curve_set = CurveSet(curve.curve)

swap = OISSwap(effective=start, maturity=period, fixed_rate=0.04, notional=100_000_000.0)
swap = OISSwap(
    effective=start, maturity=period,
    fixed_rate=par_rate(swap, curve_set).value, notional=100_000_000.0,
)
print(dv01(swap, curve_set).value)          # USD per basis point, negative for a payer
print(curve.evidence.worst_quality.value)   # 'observed'
```

## Things that will bite you

Every item here was hit while writing this package, not inferred from the
source. They are the cheapest paragraphs on this page.

**`SOFRFuture3M` takes two IMM dates, not a contract month.** The settlement
is a function of the two third Wednesdays, and a contract month tells you
which quarter rather than which Wednesdays. Use
`SOFRFuture3M.from_contract_month(2026, 3)` if a month is what you have; it
refuses any month that is not 3, 6, 9 or 12.

**Contract DV01 uses the nominal tenor, not the realised accrual.** SR3 is
USD 25.00 per basis point on a nominal 0.25 even though an IMM quarter runs
91 or 98 days; SR1 is USD 41.67 on a nominal 30/360 whatever the month
contains. `future.year_fraction` is the *realised* accrual and is the wrong
thing to size a hedge with — it is off by about one percent.

**DV01 is per basis point, not per percent.** Every risk result carries a
`unit` field for this reason. `money_duration` is the same quantity per unit
of yield, which is `dv01 * 10_000`.

**A par swap has no duration.** `effective_duration`, `effective_convexity`
and `key_rate_duration` all normalise by price, so they raise
`UndefinedDurationError` on anything priced within `1e-8` of zero relative to
notional. Use `dv01`, `key_rate_dv01` and `money_convexity` there. This is not
an edge case: a freshly struck swap is the ordinary case.

**`macaulay_duration` and `modified_duration` raise `NotImplementedError`.**
They need a single yield to maturity, so they need a bond, which is v1.1.
`effective_duration` is a different quantity and is not served under those
names.

**Key rate DV01 and the hedge's bucketed delta are different numbers.**
`key_rate_dv01` bumps curve *nodes* with tent shocks;
`HedgeResult.bucketed_delta_by_instrument` bumps instrument *quotes*. Each
sums to its own parallel — `dv01` and `HedgeResult.quote_parallel_dv01`
respectively — and the two parallels differ by roughly 365/360, because a
basis point on an ACT/360 simple forward is not a basis point on a
continuously compounded zero rate. Neither is the correct one. Compare
`hedge_ratio` against the quote basis, which is what it uses.

**A volatility is never a bare float.** `Volatility(value, units)` carries
its unit, and there is no conversion between normal and lognormal, because
there is none to have: `atm_equivalent_normal` and `atm_equivalent_lognormal`
are named for the at-the-money approximation they are, not for a conversion
they are not. The magnitude band catches what is left — normal outside
`[0.1, 1000]` bp or lognormal outside `[5%, 500%]` raises `VolUnitsError`.
The lognormal floor is 5% rather than something smaller on purpose: swaption
normal vols live at 60-150 bp, which as decimals are 0.006-0.015, so any
floor below that lets the entire population of "normal passed as lognormal"
through. `Volatility.unchecked()` exists for the genuinely extreme case.

**The model follows from the units, not from an argument.** `pricing.options`
picks Bachelier for a normal volatility and Black for a lognormal one. Pass
the volatility you have; do not convert it to reach the model you wanted.

**Black refuses a non-positive forward.** A lognormal model on a forward at
or below zero has nothing to say, so it raises `ShiftRequiredError` and names
the shift as the fix rather than returning a number. Bachelier is defined
there and is the other answer.

**SABR fills strikes inside a smile and nothing else.** `VolCube` raises
`SliceNotQuotedError` for an expiry or tenor it does not quote. Interpolating
across slices is a surface model, and v2 has not chosen one.

**SABR's expansion can run out.** At long expiry and high vol of vol the
`O(nu² T)` correction drives it negative, and it raises
`ExpansionBreakdownError` rather than returning a volatility that prices
nothing. `expansion_is_valid` asks the question without raising.

**A term rate off a futures-fitted curve is not a traded term rate.** It is
the expected average overnight rate, with no convexity adjustment. The
number is returned; `TERM_RATE_CAVEAT` and a `Degradation` marking the result
`assumed` come with it.

**Changing the interpolation does not change the fit.** Both `log_linear_df`
and `monotone_convex` reproduce the discrete forwards exactly, so both
reprice every calibration instrument. They differ in the instantaneous
forward *between* nodes, which no instrument constrains.
`curves.compare_interpolations` measures that gap rather than declaring a
winner.

**A delta does not name a strike.** FX has four conventions — spot or
forward, premium-adjusted or not — and they give four different strikes for
the same quoted number, over three hundred pips apart on USD/MXN at three
months. There is no default and `DeltaConventionError` says why: the
research gate could not establish which one the pair trades on. "At the
money" is likewise three strikes, and `ATMConvention` is explicit for the
same reason.

**Every MXN convention in this build is a guess, and says so.**
`UNRESOLVED_MXN` names them; every peso result carries one `Degradation`
per entry, so `worst_quality` reaches `ASSUMED` and anything priced on the
curve inherits it. Pass `strict_conventions=True` to refuse instead. The
gap is stored as data: resolving it shortens the tuple and changes no code.

**The engine compares hedges; it does not recommend one.** There is no
function whose name contains `recommend`, and a test scans for it.
`compare_structures` returns cost, worst case, best case and upside
participation, and `TRADE_OFF_FRAME` labels the axes without choosing a
point on them.

**A currency is carried, and two of them never mix silently.**
`DiscountCurve` and `Cashflow` both have one, defaulting to `USD` — which
is what every v1 and v2 object already was. Discounting a flow on a curve
of another currency raises `CurrencyMismatchError` before any arithmetic.
There is no conversion here at all: that needs a spot rate, a date and a
quoting convention, and `rates_engine.pricing.fx_forward` is where those are stated.

**`holidays(year)` returns dates observed *for* that year, not dates *in*
it.** With New Year's Day on a Saturday, the observed holiday is 31
December of the year before, and it lives in `holidays(next_year)`. Use
`is_business_day`, which checks the neighbouring years; do not test
membership of `holidays(day.year)` yourself. That exact shortcut was a bug
in 0.1.0 and 0.2.0.

**The Mexican calendar is `BMV`, and that is not Banxico.** It is the stock
exchange calendar, which is the one a reachable source documents. Banxico's
banking calendar is a different list and the comparison has not been made.
It also has no weekend-observance rule: a fixed-date Mexican holiday on a
Saturday is simply not observed.

**An extra that is installed at the wrong version says so.** The MCP server
is written against `mcp>=2.0,<3`, where the server class is `MCPServer`; it
was `FastMCP` in 1.x. `IncompatibleDependencyError` names the range and what
moved, and subclasses `MissingDependencyError` so one `except` still covers
both. Reporting a version mismatch as an absent package sends you to
reinstall what you already have — which this codebase did once, until CI
caught it.

**A Treasury par yield will not enter a bootstrap by accident.** Anything
whose provenance says `data_quality="proxy"` raises
`ProxySourceNotDeclaredError` unless you pass
`long_end_source="treasury_proxy"`. There is no default that admits one.

**`Evidence` is nested, not flat.** `evidence.sources` holds the evidence of
the inputs, so `hedge.evidence.sources[0].sources[0]` is the bootstrap. Read
the summary with `evidence.worst_quality` or
`rates_engine.core.diagnostics.quality_report(evidence)` rather than walking it by
hand.

**Constructing a curve with rising discount factors is allowed.** Negative
rates exist and a symmetric bump can push a one basis point forward through
zero, so the monotonicity check lives in the bootstrap, not the constructor.
Call `curve.require_monotone()` when you mean to assert it.

**The CLI's native config format is JSON.** YAML works with the `config`
extra. They are not quite interchangeable in one respect the code handles for
you: YAML parses `2026-01-15` into a `date`, JSON leaves it a string.

**The discounting evidence comes from the curve's currency.** Every price
records `discounting: collateral_rate_ois_<index>`, where the index is
`conventions.indices.collateral_index(curve.currency)`: SOFR for dollars,
TIIE de Fondeo for pesos. A peso price also carries the
`unresolved_convention:mxn_collateral_rate` degradation, so its
`worst_quality` is `assumed` even on a hand-built curve. A new `Currency`
member needs an entry in `COLLATERAL_INDEX`; `tests/test_indices.py` fails
until it has one.

**An instrument carries its index, and the index fixes its currency.**
`OISSwap(index=TIIE_FONDEO, ...)` is a peso swap that rolls on the BMV
calendar; the default `index=SOFR` is a dollar swap on SIFMA. Pricing a
swap on a curve in another currency raises `CurrencyMismatchError` -- it is
not relabelled, which is what v0.3 did. `calendar=None` means "the index's
calendar"; pass one only to override it. `IRSwap` and `CapFloor` default to
Term SOFR of their frequency, and a frequency with no published Term SOFR is
refused. A `FRA` has no frequency, so it defaults to Term SOFR 3M whatever
its period: name the index when the period is not three months.

**Instruments do not price themselves.** There is no `swap.cashflows(curve_set)`.
`rates_engine.project(instrument, curve_set)` gives the flows, `swap.fixed_cashflows()`
the fixed leg (no curve needed), `pricing.projection.float_leg` the floating
one. A product of your own prices with `pv` (and so every risk measure and
hedge) once you `project.register` it; a swap-like one also needs
`float_leg.register` for `par_rate`, `annuity` and swaptions.

**Calibrate to a swap with `SwapQuoteNode`, not `ParSwapNode`, when you have
the swap.** `SwapQuoteNode(swap, quote)` fits the pricer's own par rate, so
the curve reprices the swap exactly. `ParSwapNode` takes dates and accruals
and prices the floating leg as `P(start) - P(last payment)`, which is exact
only when every period is paid the day it ends. A SOFR OIS pays two business
days later: a curve fitted to `ParSwapNode`s built from such swaps' payment
dates misprices the one-year swap by about 5.5 bp. With no payment lag the
two nodes agree exactly. The CLI's `par_swaps` entries are swaps --
`effective`, `maturity`, `rate`, optional `frequency_months` (12) and
`payment_lag_days` (2) -- and become `SwapQuoteNode`s; the v0.3 shape
(`start`, `payment_dates`, `year_fractions`) is refused with the migration in
the message. The CLI's `price` refuses a non-dollar swap because its curve
block quotes SOFR instruments.
`SwapQuoteNode` takes an OIS only.

**Accrual periods end on adjusted dates.** Since v0.4 a schedule's period
ends are rolled by the same convention as its payments (Modified Following
by default), and the payment falls `payment_lag_days` business days after
the adjusted end -- the SOFR OIS and 2006 ISDA convention. v0.3 left period
ends unadjusted, so interest accrued to a Saturday was paid on the Monday.
`Schedule.generate(..., adjust_accrual_ends=False)` reproduces the old
schedule.

**Nothing here reads the network except `market.providers.fred`,** and that
imports `urllib` inside the call. Importing `rates_engine` opens no socket and
installs no warning filter; `tests/test_import_side_effects.py` enforces both.

## Where things live

Packages sit on layers, lowest first; a package imports only from layers
below its own, and `tests/test_layering.py` enforces it. The design is
[`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md).

**Import from `rates_engine` or from the module that defines a name.**
Subpackage `__init__` files document their layer and re-export nothing, so
`from rates_engine.curves import DiscountCurve` fails on purpose; write
`from rates_engine import DiscountCurve` or
`from rates_engine.curves.discount import DiscountCurve`.

| Package | Layer | Responsibility |
| --- | --- | --- |
| `core` | 0 | `errors` (every refusal, each with an exit code), `evidence`, `results` (`EngineResult`), `money` (`Currency`), `diagnostics` (reading an evidence chain) |
| `conventions` | 1 | Day counts, the SIFMA and BMV calendars, rolls, IMM dates, schedules; `indices` (`RateIndex`, the collateral index of each currency, `UNRESOLVED_MXN`); the shared vocabulary `Side`, `OptionKind`, `CurrencyPair` |
| `market` | 2 | Snapshots, the SOFR compounding rules, realised sigma (`estimators`), the `file`, `fred` and `banxico` providers |
| `models` | 2 | Closed forms: Black, Bachelier, Garman-Kohlhagen, SABR, the four FX delta conventions, Ho-Lee and Hull-White convexity |
| `curves` | 3 | `DiscountCurve`, the bootstrap, the four views, the dual-curve solver, monotone convex, Nelson-Siegel, the FOMC step curve, and the MXN curve with its unresolved conventions |
| `volatility` | 3 | `Volatility` and its units, the SABR cube, the vanna-volga FX smile |
| `instruments` | 3 | Terms only, each carrying its `RateIndex`: `OISSwap`, `IRSwap`, `FRA`, `SOFRFuture1M`, `SOFRFuture3M`, `CapFloor`, `Swaption` |
| `pricing` | 4 | `linear` (`pv`, `par_rate`, `annuity`, `price_on_parametric`, and `discounted_value`, the bare float for callers that reprice many times), `options` (swaptions, caps and floors), `fx_forward` (the CIP forward and its basis), `collateral` (the discounting evidence and degradations every result carries), `projection` (`project`, `float_leg`: instruments to cashflows), `calibration` (`SwapQuoteNode`) |
| `risk` | 5 | `bumps` (the one shift-and-reprice primitive, `BUMP_BP`, `tent_weights`), `sensitivities` (parallel `dv01`, key rate, duration conventions, convexity, and the stubs), `greeks` (option greeks) |
| `hedging` | 6 | `futures_strip` (`strip_hedge`, `shock_table`), `fx_structures` (`compare_structures`), `program` (the hedging policy as data) |
| `reporting` | 7 | JSON payloads and error payloads |
| `app` | 8 | The use cases: `config` (loading, dates), `builders` (config blocks to domain objects), `commands` (one function per command and `COMMANDS`, the table every interface dispatches through) |
| `cli` | 9 | `rateng bootstrap / price / hedge / describe / list-instruments / fx-forward / hedge-structures` |
| `mcp_server` | 9 | `rateng-mcp`: the same seven payloads over stdio, from the same table |

## The CLI in one line

```bash
rateng describe --json                          # capabilities, no config needed
rateng bootstrap --config c.json --json         # curve plus its four views
rateng price --config c.json --json             # pv, par, annuity, every risk measure
rateng hedge --config c.json --json             # contracts per period plus the shock table
rateng list-instruments --json                  # what this build prices, and what each needs
rateng fx-forward --config fx.json --json       # CIP forward, basis reported separately
rateng hedge-structures --config fx.json --json # eight structures, costed side by side
```

The MCP server is the same seven handlers over stdio, so a tool's answer is
byte-identical to the corresponding `--json` command. It needs the `mcp`
extra; without it, `rateng-mcp` says which extra installs it rather than
raising `ModuleNotFoundError`.

```bash
pip install "finport-ratesengine[mcp]"
rateng-mcp
```

With `--json`, stdout is exactly one document and all narration goes to
stderr. Exit code 1 means the inputs are unusable, 2 means the calculation is
impossible.
