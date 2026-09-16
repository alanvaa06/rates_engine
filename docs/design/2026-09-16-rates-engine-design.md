# Motor de tasas SOFR: diseño de arquitectura (fase 1)

*Generado: 2026-09-16 | Decisiones de Alan: espejo de `finport-optengine`; alcance fase 1 = curvas + futuros + swaps, sin swaptions | Estado: **borrador para aprobación**, sin código*

> **Método.** Convenciones tomadas de `alanvaa06/optimization_engine` v0.7.0 (pyproject, AGENTS.md, CHANGELOG, tests/, docs/ERRORS.md — leídos vía GitHub). Contenido técnico y tests golden tomados del wiki compilado hoy: [[SOFR Futures — Pricing, Convexity and Hedging Swaps]], [[Multi-Curve Framework and Collateral Discounting]], [[Forward vs Futures and the Eurodollar Convexity Bias]], [[LIBOR Transition, SOFR and Fallbacks]], [[Term Structure Models for Swaps and Swaptions]], [[Forwards, Multi-Curve and Swaptions (Post-LIBOR)]]. Todo número citado abajo tiene esa procedencia; lo que es supuesto mío está marcado **[asumo]**.

---

## 0. Qué es y qué no es

**Es:** una librería Python determinista para construir curvas SOFR, valuar futuros SOFR (SR1/SR3), OIS e IRS, medir el ajuste de convexidad futuros-vs-forward y cubrir un swap con una tira de futuros. Cada resultado devuelve el número **y la evidencia** (qué curva, qué interpolación, qué modelo de convexidad, qué σ, qué residuo de ajuste en bp, qué convención no pudo resolver).

**No es:** un agente, un modelo de IA en runtime, ni un motor de portafolio. Cero tokens en producción. La IA vive en el proceso de construcción (Claude Code + forge-master en el repo del producto, TDD), en `AGENTS.md`/`llms.txt` como superficie para agentes de código, y opcionalmente en un servidor MCP que es transporte, no inteligencia.

**Standalone:** no depende de RoRo ni de `optimization_engine`. Comparte convenciones, no código.

**Nivel:** CFA L2 term structure ([[CFA L2 Fixed Income — Term Structure and Arbitrage-Free Valuation]]) y L3 swap strategies ([[CFA L3 Derivatives — Swap Strategies]]) como marco; papers del wiki como fuente de fórmulas.

---

## 1. Principio rector heredado

De `optimization_engine`: *"an allocation is not a result until you can see what it rests on"*. Traducido:

==**Una curva no es un resultado hasta que ves sobre qué descansa.**== Cada `price()`, `bootstrap()` o `hedge()` devuelve un objeto con `value` y `evidence`:

| Campo de evidencia | Qué contiene |
|---|---|
| `instruments_used` | Lista con fuente, fecha, valor, y cuáles se descartaron y por qué |
| `fit_residuals_bp` | Residuo por instrumento tras el bootstrap |
| `interpolation` | Método y nodos |
| `convexity_model` | `none` / `ho_lee` / `hull_white` + σ, κ, y de dónde salió σ |
| `conventions` | Day count, calendario, roll, lag de pago, con `unresolved: [...]` si algo se asumió |
| `warnings` | Degradaciones **reportadas, no lanzadas** (p. ej., proxy Treasury en vez de OIS) |

Lo que no se puede calcular honestamente **se rehúsa** (raise), nunca se rellena. Contrato de refusals en `docs/ERRORS.md`, igual que en optengine.

---

## 2. Alcance por fases

| Fase | Módulos | Definition of Done |
|---|---|---|
| **1 (este doc)** | `conventions`, `market`, `instruments` (OIS, IRS, SR1, SR3, FRA), `curves`, `convexity`, `pricing`, `hedging`, `diagnostics`, `reporting`, `cli` | Reproduce el whitepaper CME 2025 dentro de tolerancia (§5); settlement SR1/SR3 vs CME < 0.1 bp; bootstrap dual-curva secuencial vs simultáneo con diferencia reportada en bp; `rateng bootstrap/price/hedge --json` |
| 2 | `swaptions` (Black lognormal y Bachelier normal en bps; SABR después) | Cubo expiry×tenor×strike con vol normal; paridad payer/receiver; test contra [[Term Structure Models for Swaps and Swaptions]] |
| 3 | `fx` (CIP + basis cross-currency, Garman-Kohlhagen, estructuras CFA L3 Reading 19) | Requiere curva MXN (TIIE) — gap del vault, research previo obligatorio |
| Opcional | `mcp_server` | Mismos payloads que `--json`; extra `[mcp]`, Python ≥ 3.10 |

Swaptions **fuera** de fase 1 por decisión de Alan (2a). Ventaja: fase 1 no necesita superficie de vol de ningún tipo — σ de convexidad viene de vol realizada de SOFR o de input explícito.

---

## 3. Módulos e interfaces (fase 1)

Layout `src/rates_engine/` (nombre pendiente, §8). Un módulo = una responsabilidad = un archivo de tests.

### 3.1 `conventions`
- `DayCount`: `ACT_360`, `ACT_365F`, `THIRTY_360`. Enum, no strings sueltos.
- `BusinessDayConvention`: `FOLLOWING`, `MODIFIED_FOLLOWING`, `PRECEDING`.
- `Calendar`: protocolo `is_business_day(date)`; implementación `SIFMA_US` en fase 1. **Regla SOFR:** en día no publicado se repite la última tasa publicada (CME/NY Fed, citado en el wiki).
- `Schedule`: genera fechas de fixing/pago/accrual; `imm_dates(year)` para terceros miércoles.
- **Refusal:** convención no soportada → `UnsupportedConventionError`. Nunca "default silencioso".

### 3.2 `market`
- `MarketSnapshot(date, series: dict[str, Series], provenance: dict[str, Provenance])`.
- Proveedores: `fred` (`SOFR`, `SOFR30DAYAVG`, `SOFR90DAYAVG`, `SOFRINDEX`, `EFFR`, `DGS2/3/5/10`), `cme_settlements` (SR1/SR3, CSV público con retraso), `ice_swap_rate` (par OIS si accesible), `file` (CSV/Parquet). Mismo patrón que `optimization_engine.ingest`: un panel, muchos proveedores, provenance por serie.
- **Refusal:** fixing faltante en fecha de negocio → `MissingFixingError`. No se interpola un fixing.

### 3.3 `instruments`
Dataclasses inmutables con `cashflows(curve_set) -> list[Cashflow]`:
- `OISSwap(effective, maturity, fixed_rate, notional, pay_lag=2)`; flotante compuesto diario ACT/360 in arrears.
- `IRSwap(...)` fijo vs Term SOFR 3M (proyección en curva de tenor, descuento OIS).
- `SOFRFuture1M(contract_month)`: settlement = 100 − media aritmética del mes calendario (eq. 14 del wiki).
- `SOFRFuture3M(imm_start, imm_end)`: settlement = 100 − compuesto ACT/360 entre terceros miércoles (eq. 18).
- `FRA(start, end, rate)`: valuada en multi-curva; **la tasa FRA no es el forward simple de la curva de descuento** (Bianchetti / Mercurio en [[Multi-Curve Framework and Collateral Discounting]]).
- DV01 por contrato: SR3 = USD 25/bp (confirmado); SR1 = USD 41.67/bp **[asumo — spec CME no leída, marcado en el wiki]**.

### 3.4 `curves`
- `DiscountCurve(nodes, dfs, interpolation)`; interpolaciones: `log_linear_df` (default), `monotone_convex` (v2). Exponer `df(t)`, `forward(t1, t2, day_count)`, `zero(t)`.
- **Cuatro vistas y conversiones (añadido 2026-09-16 a petición de Alan):** `zero_curve(compounding, day_count)`, `par_curve(tenors, frequency, day_count)`, `forward_curve(tenor)`; relaciones de no-arbitraje del temario ([[CFA Fixed Income — Valuation]], [[CFA L2 Fixed Income — Term Structure and Arbitrage-Free Valuation]]) como tests de roundtrip par→cero→par. Ninguna tasa se exporta sin su convención de composición.
- **Descuento a la tasa de fondeo/colateral:** los futuros y swaps colateralizados se descuentan en la curva OIS-SOFR porque el colateral se remunera a SOFR (Fujii-Shimada-Takahashi, Piterbarg en [[Multi-Curve Framework and Collateral Discounting]]); la evidencia lo declara. Instrumentos de bootstrap: stub de SOFR realizado → futuros SR1/SR3 ajustados por convexidad → swaps OIS par (PRD-001 US-3, US-5).
- `bootstrap_ois(snapshot, instruments) -> BootstrapResult` — secuencial, un nodo por instrumento.
- `solve_dual_curve(ois_instruments, tenor_instruments, basis_instruments, mode="simultaneous"|"sequential") -> DualCurveResult` — `scipy.optimize.least_squares` sobre todos los nodos; residuales por instrumento; `basis_adjustment_bp` (Bianchetti eq. 20) y `sequential_vs_simultaneous_bp` por tenor.
- Evidencia obligatoria: residuos, condición del jacobiano, nodos, instrumentos descartados.

### 3.5 `convexity`
- `convexity_adjustment(future, model, sigma, kappa=None) -> ConvexityResult` en bp.
- Modelos: `ho_lee` → `½σ²T₁T₂`; `hull_white` (κ, σ) → fórmula Henrard como la transcriben Skov-Skovmand (eq. 74–75) — **marcada en el wiki como no leída en Henrard directamente**.
- `sigma` fuentes: `explicit`, `realized_sofr(window)`. Nunca implícita en fase 1 (no hay opciones).
- Evidencia: modelo, σ, κ, fuente de σ, y la advertencia del wiki: calibrar solo ATM sobreestima 10–25% (Romero-Bermúdez-Turfus) — aplica cuando en fase 2 haya vol implícita.

### 3.6 `pricing`
- `pv(instrument, curve_set)`, `par_rate(swap, curve_set)`, `annuity(swap, curve_set)`.
- `dv01(instrument, curve_set, bump_bp=1, mode="parallel"|"key_rate")` por bump-and-reprice; devuelve también qué nodos movió.

### 3.7 `hedging`
- `strip_hedge(swap, futures_strip, curve_set) -> HedgeResult`: contratos por periodo IMM, DV01 del swap vs de la tira, ratio.
- `shock_table(hedge, shocks_bp=(-100,-50,-25,-10,10,25,50,100)) -> DataFrame`: P&L swap, P&L tira, neto, DV01 post-shock. ==El neto ≠ 0 es la convexidad en dólares; dividido entre DV01 es la convexidad en bp== — el número que el whitepaper CME no da y el wiki propone como experimento.

### 3.8 `diagnostics`, `reporting`, `cli`
- `diagnostics.evidence(result)`: ensambla §1.
- `reporting.payloads`: JSON con `schema_version`, `null` para ausentes, nunca key faltante. Fallo antes de resultado → `{error, exit_code}` en stdout, traceback en stderr (regla de optengine 0.5.1).
- `cli`: `rateng bootstrap`, `rateng price`, `rateng hedge`, `rateng describe`, todos con `--json`; narración humana a stderr.

---

## 4. Datos (fase 1, todo gratis)

| Necesidad | Fuente | Nota |
|---|---|---|
| Fixings SOFR, promedios, índice | FRED | Diario, sin API key |
| Settlements SR1/SR3 | CME público (retraso) / Nasdaq Data Link `CHRIS/CME_SR3` si sigue | Histórico limitado; el wiki lo flaggea |
| Par OIS SOFR | ICE Swap Rate | Acceso a confirmar; si no, **proxy Treasury `DGS*` con warning** — el wiki advierte que el swap spread domina |
| Fechas FOMC | federalreserve.gov | Para experimento Heitfield-Park (§7) |
| Calendario SIFMA | Codificado | Mantener como dato, no como código |

---

## 5. Tests como contrato

Filosofía de optengine: `test_analytical_rigor`, `test_no_silent_swallow`, `test_conventions`, `test_backtest_honesty`. Aquí:

### 5.1 Golden (números publicados, del wiki)

| Test | Fuente | Tolerancia |
|---|---|---|
| Hedge 2y OIS 100M con tira SR3 → **779 contratos**, cupón IMM **3.3304%** | CME/Rogerson 2025 | ±2 contratos, ±0.5 bp |
| Shock −100 bp → P&L neto **+22,292**; DV01 **19,480 → 19,921** | CME 2025 | ±3% (los inputs de curva del whitepaper se reconstruyen, no se copian) |
| Settlement SR1/SR3 de contratos expirados vs final settlement CME | CME + FRED | < 0.1 bp |
| Ho-Lee `½σ²T₁T₂` vs tabla de Hull | Hull (verificada solo en secundarias) | exacta |
| Ajuste material solo > 2 años | Skov-Skovmand | cualitativo: adj(1y) < 1 bp, adj(5y) > adj(2y) |
| Basis swap sintético reproducido por `BA_fd` | Bianchetti eq. 20 | < 0.1 bp |
| Secuencial vs simultáneo ≈ 0 cuando tenor = Term SOFR (basis ≈ 0); pocos bp en el largo plazo cuando basis ancha | Ametrano-Bianchetti / wiki | cualitativo |

### 5.2 Propiedades (hypothesis)
- DFs decrecientes y positivos; forwards positivos salvo que los inputs los impliquen (entonces refusal).
- Swap creado a `par_rate` tiene `pv == 0` ± 1e-8.
- `dv01` de receiver > 0 y de payer < 0, misma magnitud.
- Roundtrip: bootstrap → re-precio de los instrumentos de entrada → residuo 0.
- Idempotencia: `price()` dos veces = mismo resultado (optengine tuvo el bug de Black-Litterman no idempotente; se prueba desde el día 1).

### 5.3 Refusals
- Fixing faltante, convención desconocida, curva con nodos no monótonos, σ negativa → excepción nombrada. Nunca warning.
- `test_no_silent_swallow`: ningún `except: pass` en `src/`.

### 5.4 Contrato de superficie
- `test_payloads`: cada payload tiene `schema_version`; `null` en vez de key ausente.
- `test_import_side_effects`: importar no instala filtros ni lee red.
- Docstring coverage 100% de nombres públicos, con unidades (bp, USD/bp, años ACT/360).

---

## 6. Repo, empaquetado, CI (espejo de optengine)

- `src/` layout, `pyproject.toml` con `dependencies = [numpy, pandas>=2.2, scipy]` **solo**; extras `data` (yfinance no aplica; `pyarrow`), `mcp`, `dev` (pytest, hypothesis, ruff, mypy con allowlist que solo puede encogerse), `docs` (pdoc).
- `AGENTS.md` + `llms.txt` desde el commit 1: "las cosas que muerden" (e.g., "`SOFRFuture3M` toma fechas IMM, no mes de contrato"; "DV01 es por bp, no por %").
- `docs/ERRORS.md` (contrato de refusals), `docs/RESEARCH.md` (mapa a los artículos del wiki con las fórmulas y su estado leído/abstract), `CHANGELOG.md` Keep-a-Changelog, `docs/RELEASING.md` Trusted Publishing.
- CI: ruff, matriz 3.9–3.12, core-install job (ninguna extra se cuela), CLI smoke, docs `--strict`.
- **Build con forge-master en el repo del producto, nunca en el vault** (CLAUDE.md del vault lo prohíbe).

---

## 7. Dónde entra la IA sin tokens en runtime

1. **Construcción:** PRD con ACs Given/When/Then derivados de §5 → plan → forge-run con TDD Iron Law y revisión independiente para fases heavy (`curves.solve_dual_curve`, `hedging.shock_table`). El showcase es *el proceso* documentado: papers → wiki → ACs → tests → código.
2. **Superficie agent-facing:** `--json` + `AGENTS.md`; MCP opcional. Ningún LLM se invoca desde la librería.
3. **ML offline determinista (fase 1.5, opcional):** curva escalonada Heitfield-Park entre fechas FOMC ajustada a SR1+SR3 por mínimos cuadrados; comparación con SOFR realizado. Es `scipy`, no un modelo de lenguaje.
4. **Investigación futura (no fase 1):** re-estimar Skov-Skovmand AFNS-3 con Kalman sobre SR1/SR3 2018–26; el wiki lo lista como experimento avanzado.

---

## 8. Decisiones (cerradas 2026-09-16 por Alan)

| # | Decisión | Elegido |
|---|---|---|
| 1 | Nombre | Distribución **`finport-ratesengine`**, import **`rates_engine`**, CLI **`rateng`** |
| 2 | Repo | Repo nuevo **`alanvaa06/rates_engine`**; comparte convenciones con optengine, no código |
| 3 | Interpolación default | **Log-lineal en DF**; monotone-convex como opción |
| 4 | MCP | **Fase 2**, extra `[mcp]`; fase 1 solo `--json` |

---

## 9. Riesgos y gaps conocidos

- **Inputs del whitepaper CME:** el wiki reporta outputs (779, +22,292, DV01) pero la curva exacta de entrada del documento se reconstruye; por eso la tolerancia de 3%, no exactitud.
- **OIS par gratis:** incierto. Proxy Treasury contamina con swap spread — [[Swap Spreads — Credit, Duration Demand and Limits to Arbitrage]] explica por qué hoy es negativo y variable.
- **Fórmula Hull-White de convexidad:** transcrita vía Skov-Skovmand, no leída en Henrard 2018.
- **SR1 tick:** asumido.
- **Calendarios:** SIFMA codificado a mano en fase 1; riesgo de holiday mal cargado → test contra settlements reales lo detecta.
- **MX/TIIE:** nada en el vault; fase 3 requiere research (transición TIIE 28 → TIIE de Fondeo, convenciones MXN).

---

## Fuentes del vault

[[SOFR Futures — Pricing, Convexity and Hedging Swaps]] · [[Multi-Curve Framework and Collateral Discounting]] · [[Forward vs Futures and the Eurodollar Convexity Bias]] · [[LIBOR Transition, SOFR and Fallbacks]] · [[Term Structure Models for Swaps and Swaptions]] · [[Swap Spreads — Credit, Duration Demand and Limits to Arbitrage]] · [[Swap Hedging, Central Clearing and XVA]] · [[Forwards, Multi-Curve and Swaptions (Post-LIBOR)]] · [[CFA L2 Fixed Income — Term Structure and Arbitrage-Free Valuation]] · [[CFA L3 Derivatives — Swap Strategies]] · [[Forge Master (forge-master)]] · [[Claude Code OS for Business (nateherk)]]

Repo de referencia: `alanvaa06/optimization_engine` v0.7.0 (`finport-optengine`), leído 2026-09-16.
