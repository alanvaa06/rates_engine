# Research gate for PRD-003: what this environment can establish about MXN, and what it cannot

> Run 2026-09-18, before plan-design, as PRD-003's Constraints require. The
> vault does not cover TIIE or MXN conventions; this is the attempt to close
> that gap from primary sources, and the record of how far it got.

The short version: **the machinery of v3 is buildable and the Mexican
conventions are not verifiable from here.** Those are separable, and the
PRD already contains the mechanism for keeping them separate — AC-1.4's
`unresolved_conventions`. This note says exactly which side of the line
each acceptance criterion falls on, so that plan-design can build the first
group honestly and mark the second rather than guessing at it.

## What is reachable

Egress is limited to package registries and GitHub by organisation policy —
the same limit that left v1's CME goldens skipped. Probed directly:

| Source | Wanted for | Result |
| --- | --- | --- |
| `banxico.org.mx` (SIE API and site) | TIIE fixings, series IDs, banking calendar, conventions | 403 at the proxy |
| `isda.org` | day-count definitions | 403 |
| `cmegroup.com` | USD/MXN futures and FX conventions | 403 |
| `bis.org` | CIP and cross-currency basis background | 403 |
| `en.wikipedia.org` | anything | 403 |
| `raw.githubusercontent.com`, GitHub API | QuantLib source | 200 |
| `pypi.org` | packages | 200 |

QuantLib is therefore the one substantive source available, and it is a
better one than that framing suggests: its source can be **read**, not
merely cited. That is a stronger position than several rows already marked
"Secondary" in `docs/RESEARCH.md`, where the formula was transcribed from a
paper nobody here has opened.

## What QuantLib establishes, and what it does not

**The calendar — with a caveat that matters.** `ql/time/calendars/mexico.cpp`
carries thirteen rules: New Year, Constitution Day (fixed 5 February to
2005, first Monday thereafter), Benito Juárez's birthday (fixed 21 March to
2005, third Monday thereafter), Holy Thursday, Good Friday, Labour Day,
National Day, Inauguration Day (1 October, every sixth year from 2024), All
Souls' Day, Revolution Day (fixed 20 November to 2005, third Monday
thereafter), Our Lady of Guadalupe, and Christmas.

The caveat: the implementation class is named `BmvImpl` and reports itself
as "Mexican stock exchange". **This is the BMV calendar, not Banxico's
banking calendar.** AC-1.2 asks for "el calendario mexicano (Banxico)".
They are different lists and need not agree, so sourcing from QuantLib
answers a neighbouring question rather than the one asked. Building it and
labelling it Banxico would be the exact failure this package exists to
avoid: a plausible number with the wrong name on it. The calendar should be
named for what it is, and the Banxico diff recorded as `[manual-check]` —
the same treatment SIFMA's Saturday-observance rule got in v1.

**TIIE: nothing.** A code search across the whole QuantLib repository for
`TIIE` returns zero hits. There is no index class, no day count, no tenor.
The day count, the 28-day period, the distinction between TIIE 28 and TIIE
de Fondeo, and the Banxico SIE series identifiers are **not established by
anything reachable from here**.

## The honest split

**Establishable now, from closed forms or readable sources:**

- AC-1.2, as the BMV calendar under its own name, with the Banxico
  comparison outstanding.
- AC-2.1 — covered interest parity is an identity; it needs no convention.
- AC-2.2, AC-2.3, AC-2.4 — the basis decomposition, its inversion and the
  plausibility refusal are all arithmetic over inputs the caller supplies.
- AC-3.1 — Garman-Kohlhagen put-call parity is a closed-form identity.
- AC-3.2 — vanna-volga. Prototyped here on a plausible USD/MXN smile
  (S = 18.50, T = 0.25, r_MXN = 9.50%, r_USD = 4.20%, ATM = 11.50%,
  RR25 = 1.80%, BF25 = 0.35%): it reproduces the market price at the 25Δ
  put, the ATM and the 25Δ call to `0.00e+00`. Exact reproduction of the
  three pillars is what the criterion asks for, and it holds.
- AC-3.3, AC-3.4, AC-3.5 — delta conventions as an explicit enumeration,
  the refusal when one is not declared, and bumped greeks. Note that the
  prototype above needed *no* MXN convention to produce strikes: it stated
  one. The gap is in the **default**, not in the machinery.
- AC-4.1 to AC-4.5 — the hedge-structure comparator is arithmetic over
  option prices plus an API-surface property.
- AC-5.1, AC-5.2 — config validation and the band audit.

**Not establishable, and not to be invented:**

- AC-1.1 — TIIE day count and the 28-day period. The PRD already marks this
  `[asumo, verificar en research previo]`; the research ran and did not
  resolve it.
- AC-1.3 — the TIIE 28 versus TIIE de Fondeo distinction can be *carried*
  (the payload marks which benchmark a curve used, and refuses to mix them
  unmarked), but which conventions attach to each cannot be stated.
- AC-3.3's **default** convention for USD/MXN. The PRD marks it
  `[asumo ... verificar en research]`. Unresolved, so there should be no
  default: the criterion's own refusal, `DeltaConventionError`, is the
  answer.
- The Banxico SIE series identifiers for either benchmark.

AC-1.4 is the seam the PRD itself built for this, and plan-design should
run the phase along it: evidence carries `unresolved_conventions` naming
each unverified convention, and `strict=True` refuses rather than
proceeding.

## The finding that outranks the research gap

`grep -rn "currency" src/` returns two hits, both prose inside docstrings.
There is no currency type, no currency on `DiscountCurve`, none on
`Cashflow`, none on any instrument. USD has been implicit everywhere
because there has only ever been one currency.

PRD-003 introduces a second. In a currency-blind engine, nothing stops a
caller discounting MXN cashflows on the USD curve, or adding a USD present
value to an MXN one and printing the sum. Both produce a number, neither
raises, and the evidence chain — which is the entire argument of this
package — carries no trace of the mistake.

This is the same class of error as v2's volatility units, and it has the
same fix: the type carries the fact, so the mistake cannot be made rather
than being caught afterwards. `Volatility(value, units)` is the precedent.
The analogue is a currency on `DiscountCurve` and on `Cashflow`, with
addition across currencies refused.

It belongs in plan-design as task zero, before any TIIE code, because
retrofitting it after four user stories of MXN work is strictly more
expensive. Defaulting to USD keeps every existing v1 and v2 call working
unchanged, which satisfies the Constraint that no USD curve changes — the
change is additive. It is still a decision rather than a detail, and it is
recorded here as one.

## The two open questions in PRD-003

The PRD marks its own recommendations, and both hold up against what this
research found:

1. **TIIE 28 *and* TIIE de Fondeo, each explicitly marked** — (a). AC-1.3
   already requires the payload to distinguish them, so supporting only one
   would make that criterion vacuous.
2. **Vanna-volga rather than reusing v2's SABR** — (a). Vanna-volga
   reproduces the three quoted points exactly, measured above at
   `0.00e+00`. SABR does not have that property by construction: it fits a
   smile, it does not interpolate pillars.

## What would close the gap

One person with a browser, half an hour: Banxico's SIE series pages for
TIIE 28 and TIIE de Fondeo (day count, publication rule, series ID),
Banxico's published banking-calendar list for 2026, and any USD/MXN
broker's convention sheet for the delta basis. None of it is obscure; it is
simply behind an egress policy. Until then the code should say
`unresolved_conventions` and mean it.
