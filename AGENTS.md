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
thing to size a hedge with — it is off by about one percent. `strip_hedge`
sizes each contract at its own: `FuturesNode(contract="SR1")` or `"SR3"`,
or `None` to read it off the period (a calendar month is SR1, an IMM
quarter SR3). `contract_dv01` applies to every contract and is refused if
it contradicts one the node declares or its period names; a period that is
neither is sized as SR3, or at the caller's `contract_dv01`, and carries a
`futures_contract_assumed` degradation until its contract is declared; a
declared contract on a rolled period must run within three days of its own
length. On a strip that mixes SR1 and SR3, `total_contracts` and
`contract_dv01` are `None`: read `total_contracts_by_contract`.

**DV01 is per basis point, not per percent.** Every risk result carries a
`unit` field for this reason. `money_duration` is the same quantity per unit
of yield, which is `dv01 * 10_000`.

**A par swap has no duration.** `effective_duration`, `effective_convexity`
and `key_rate_duration` all normalise by price, so they raise
`UndefinedDurationError` on anything priced within `1e-8` of zero relative to
notional. Use `dv01`, `key_rate_dv01` and `money_convexity` there. This is not
an edge case: a freshly struck swap is the ordinary case. Away from zero,
`key_rate_duration` is in years, and its buckets sum to
`effective_duration`.

**`macaulay_duration` and `modified_duration` raise `NotImplementedError`.**
They need a single yield to maturity, so they need a bond, which is v1.1.
`effective_duration` is a different quantity and is not served under those
names.

**Key rate DV01 and the hedge's bucketed delta are different numbers.**
`key_rate_dv01` bumps curve *nodes* with tent shocks;
`HedgeResult.bucketed_delta_by_instrument` bumps instrument *quotes*. Each
sums to its own parallel — `dv01` and `HedgeResult.quote_parallel_dv01`
respectively — and the two parallels differ by a factor of about
(365/360)/(1 + F tau) — 1.00365 on the README strip, not 365/360 — because
one basis point on the continuously compounded ACT/365F zero rate moves an
ACT/360 simple forward by (1 + F tau) x 360/365 basis points. Neither is
the correct one. Compare
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
FX is the exception: `StructureQuote` takes a float FX vol in [0.2%, 500%],
since FX has no normal-vol quote to confuse it with.

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

**A caplet expires at its fixing, and a seasoned cap needs its fixings.**
Term SOFR fixes two business days before its period; pass
`fixings=MarketSnapshot(...)` (series `TERM_SOFR_3M`) to value periods
already fixed. A period fixing on `as_of` uses that day's fixing if you
pass it and the curve's forward if you do not; the period's
`fixing_source` says which, and the forecast is not a degradation. A
snapshot without that series is refused once a period has fixed or fixes
on `as_of`; a strip that fixes later never reads it. An event
dated `as_of` has happened, as in QuantLib by default: a payment that day
is not valued, and a swaption expiring that day is refused like an earlier
expiry. `sabr.lognormal_vol` with a shift needs `shifted_black=True`.

**A term rate off a futures-fitted curve is not a traded term rate.** It is
the expected average overnight rate, with no convexity adjustment. The
number is returned; `TERM_RATE_CAVEAT` and a `Degradation` marking the result
`assumed` come with it.

**Changing the interpolation does not change what reprices.** Both
`log_linear_df` and `monotone_convex` reprice every calibration instrument
— monotone convex because the bootstrap sweeps it to a fixed point
(`evidence.fields["iterations"]`), since its forward at a node depends on
the next interval. On a futures strip the node factors are identical under
both; on swaps paid with a lag they are not (up to 0.58 bp of zero rate on
a 12-swap SOFR strip). They differ most in the instantaneous forward
between nodes, which no instrument constrains.
`curves.compare_interpolations` measures the gap rather than declaring a
winner. A monotone convex curve with a forward near zero has a node at
zero between intervals of opposite sign, so a DV01 whose down-bump crosses
zero differs from a small-bump DV01 by about 1e-5 relative.

**A delta does not name a strike.** FX has four conventions — spot or
forward, premium-adjusted or not — and they give four different strikes for
the same quoted number, over three hundred pips apart on USD/MXN at three
months. There is no default and `DeltaConventionError` says why: the
research gate could not establish which one the pair trades on. "At the
money" is likewise three strikes, and `ATMConvention` is explicit for the
same reason.

**Every MXN convention in this build is a guess, and says so.**
`UNRESOLVED_MXN` names them; every peso result carries one `Degradation`
per entry it rests on (a TIIE de Fondeo curve does not carry
`tiie_28_fixing_lag`), so `worst_quality` reaches `ASSUMED` and anything
priced on the curve inherits it. Pass `strict_conventions=True` to refuse
instead. The gap is stored as data: resolving it shortens the tuple and
changes no code.

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

**`basis_bp` is a zero-coupon spread, not the basis a screen quotes.**
`forward_from_curves` writes the outright as `cip_forward * exp(+b tau)`,
with tau the ACT/365F span from the spot date to delivery, which is the
same as discounting the quote leg on `P_d(T) * exp(-b tau)`: a
flat, continuously compounded spread over the domestic OIS zero rate at
that one tenor. A cross-currency swap quotes its basis as a *par* spread
on the coupon leg of a swap that exchanges notional at both ends, and a par
spread differs from a zero spread the way a par rate differs from a zero
rate — close at one period, apart beyond it. Nothing here converts one
into the other, because nothing here builds the curve that would: there is
no cross-currency swap instrument, no notional exchange, no discount curve
for peso flows under dollar collateral, and no calibration node that strips
such a curve from basis quotes. `solve_dual_curve` is not that either — its
two curves are OIS and Term SOFR in one currency. `implied_basis` reads the
zero spread off one forward, and that is as far as this build goes. The
piece it would plug into exists: `CurveSet` already projects on one curve
and discounts on another of the same currency.

**An FX forward runs from the spot date, not from `as_of`.** USD/MXN
counts two business days on which both the Federal Reserve and BMV are
open (`USDMXN.spot_date(as_of)`): for the peso a US holiday on T+1 delays
the spot (`JOINT_LAG_CURRENCIES`), so 2026-11-25 settles on 11-30. Dollars
settle on `FEDERAL_RESERVE`, not `SIFMA_US`, and
`CurrencyPair(calendars=...)` is keyed by `Currency`. Parity is
`S [P_f(T)/P_f(spot)] / [P_d(T)/P_d(spot)]`; measuring from `as_of` was 76
to 78 pips too high. A delivery on or before the spot date raises. The
CLI's `r_domestic`/`r_foreign` are continuously compounded ACT/365F
decimals.

**`worst_case_rate` and `best_case_rate` are `None` on an open side**,
worked out from the legs over every spot rather than read off the grid.

**`holidays(year)` returns dates observed *for* that year, not dates *in*
it.** No SIFMA holiday crosses a year boundary any more (a Saturday New
Year's Day is not moved to 31 December; SOFR is published that day), but
an observance rule can push one across, so use `is_business_day`, which
checks the neighbouring years; do not test membership of
`holidays(day.year)` yourself. That exact shortcut was a bug in 0.1.0 and
0.2.0. It lists only weekdays the market closes, on every calendar: a
Saturday New Year's Day or Veterans Day on `SIFMA_US`, or a fixed-date
Mexican holiday on a weekend on `BMV`, is not in it.

**The Mexican calendar is `BMV`, and that is not Banxico.** It is the stock
exchange calendar, which is the one a reachable source documents. Banxico's
banking calendar is a different list and the comparison has not been made.
It also has no weekend-observance rule: a fixed-date Mexican holiday on a
Saturday is simply not observed, and is not in `holidays(year)`.

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

**A snapshot serves nothing after its `as_of`.** `fixing`, `compounded` and
`averaged` raise `MissingFixingError` for a business day after the
valuation date, and `require` returns the series cut there. Load a history
once and wrap it in a `MarketSnapshot` per backtest date; `snapshot.series`
keeps what was loaded. The fixing dated `as_of` is served, a one-day
look-ahead for SOFR.

**Units are an argument, unless the layout says them.** `percent=` wins
whenever it is given. Without it, `load_series_csv` reads FRED's download
(`observation_date,<ID>`, columns named or not) in FRED's units, as
`fred.fetch_series` reads it: the overnight rates, SOFR averages and
`DGS*` are divided by 100, `SOFRINDEX` is not, anything else needs
`percent=`. This package's `date,value` is decimals. FRED's older
`DATE,<ID>` header and any other named columns holding a rate raise
`UnsupportedConventionError`: SOFR at 0.05% read as a decimal is a
believable 5%, so nothing in the values can say which it was.

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

**Every key in a config file must be documented, and the whole file is
checked.** `rateng describe --json` lists every key under `config`, with
its units, its default and the commands that read it. A key it does not
list is refused by path with the closest match, whichever command reads
the file. `_` keys are comments, and switch nothing off in a block that
holds anything else. A list of numbers is a JSON list,
`"shocks_bp": [25]`, never `"25"`. `null` means absent only where the
default is `null`, an empty optional block is no block, and
`curve.convexity` names its `model`. Adding a key is one entry in
`app/config.py`'s `CONFIG_SCHEMA`, and `tests/test_app_config.py` fails
until some command reads it.

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

**A trade that has started needs its fixings, everywhere.** `pv`,
`par_rate`, every risk measure and `strip_hedge` take
`fixings=MarketSnapshot(...)` (series named after the index), and
`shock_table` reprices with the hedge's; the CLI and MCP read the
`fixings` block of the config. Without them a seasoned trade is refused
with `MissingFixingError`. Paid flows count for nothing. A fixing dated
before `as_of` is required; one dated `as_of` is read if you pass it and
forecast if you do not, as QuantLib does by default, without a
degradation, and a swap or FRA records it in `fixings_dated_as_of`. A
snapshot without the index's series is refused even for a term fixing
dated `as_of`. A forward before the curve's `as_of` is refused too, and so
is a curve entry that needs history: a started futures contract goes in
`curve.stub` (a `RealizedStubNode`), and a par swap must start on or after
`as_of`. The `fixings` block prices the swap, never the curve. A started
swap's hedge is checked from `as_of`, with the stub covering `as_of` to
its end. The fixing lag lives on `RateIndex.fixing_lag_days`, so an IRS, a
FRA and a caplet on one period fix on one day. The fixings used are in the
evidence as `realised_fixings`, chained once into every risk measure. In a
config, rates are decimals and dates SIFMA business days, and the latest
fixing must sit near the curve's overnight rate.

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
the adjusted end -- the SOFR OIS and 2006 ISDA convention. The effective
date is rolled by the same convention, and a first roll date the
convention pulls back onto it is dropped rather than left as a zero-day
period, as QuantLib's `Schedule` does. v0.3 left period ends unadjusted,
so interest accrued to a Saturday was paid on the Monday. `span` reports
those adjusted dates -- an `OISSwap` written from Saturday 30 May 2026
spans from Friday the 29th -- so a hedge's coverage check and a swaption's
exercise date are tested against the day the swap starts accruing. A gap
at either end of the hedge made only of days the swap's calendar closes is
tolerated and listed in the evidence (`coverage_tolerated_gaps`). A
rolled start can come before the stated effective date; valued in between,
the swap has begun and `pv` asks for its fixings. A schedule the convention
collapses to no period raises `ConventionError` when the instrument is
built.
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
| `conventions` | 1 | Day counts, the SIFMA, Federal Reserve and BMV calendars (`settlement` holds the payment-system ones FX settles on), rolls, IMM dates, schedules; `indices` (`RateIndex`, the collateral index of each currency, `UNRESOLVED_MXN`); the shared vocabulary `Side`, `OptionKind`, `CurrencyPair` |
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
impossible. `--help --json` is a `Usage` document, and a command line the
parser rejects is a `ConfigurationError` document with exit code 1.
