# PRD-002: v2 — swaptions, caps/floors, cubo de vol, modelos de curva y MCP

> Escrito 2026-09-16. Depende de PRD-001 completo (v0.1.0 publicado). Formato forge-master. **Estado: borrador; requiere aprobación de Alan.** Supuestos marcados **[asumo]**.

## Goal
Publicar `finport-ratesengine` v0.2: añadir opciones de tasa vanilla (swaptions payer/receiver, caps/floors) con Black lognormal y Bachelier normal en bps, un cubo de vol expiry×tenor×strike con SABR/shifted-SABR como interpolador, modelos de curva paramétricos (Nelson-Siegel/AFNS, curva escalonada FOMC de Heitfield-Park) como alternativa al bootstrap, interpolación monotone-convex, y un servidor MCP que expone los mismos payloads que `--json`. Sigue sin IA en runtime; el MCP es transporte.

## Non-Goals
- Bermudan/American swaptions, CMS, exóticos de tasa, HJM/LMM completos (solo se usan sus fórmulas cerradas de convexidad ya en v1).
- Calibración de SABR a datos de mercado en vivo (v2 calibra a un cubo dado por el usuario o fixture); vanna-volga (es FX, v3).
- Vol implícita como σ de convexidad en producción sin advertencia (se permite, pero la evidencia arrastra la advertencia Romero-Bermúdez-Turfus del 10–25%).
- Moneda distinta de USD.
- XVA, márgenes, clearing.
- Cualquier LLM invocado por la librería; el MCP no razona, sirve.

## User Stories

### US-1: Swaptions con Black y Bachelier
As a quant, I want valuar payer/receiver swaptions europeas con Black (lognormal) y Bachelier (normal, σ en bps anuales), so that hable el idioma post-tasas-negativas del mercado.
- AC-1.1: Given forward swap rate $F$, strike $K$, anualidad $A$, expiry $T$ y σ normal, When se valúa con Bachelier, Then $V_{payer} = A[(F-K)N(d) + \sigma\sqrt{T}\,n(d)]$, $d = (F-K)/(\sigma\sqrt T)$, y la paridad $V_{payer} - V_{receiver} = A(F-K)$ se cumple a 1e-10.
- AC-1.2: Given σ lognormal, When se valúa con Black, Then coincide con el pricer Black de futuros/IR options del temario ([[CFA L2 Derivatives — Pricing and Valuation]]) en un fixture de 20 casos.
- AC-1.3: Given un precio de swaption, When se invierte a vol normal y a vol lognormal, Then ambas inversiones reproducen el precio a 1e-8 y la evidencia reporta ambas con la regla ATM `σ_normal ≈ σ_lognormal × F` como chequeo ([[Volatility, Greeks and Option Strategy Practice (2026)]]).
- AC-1.4: Given una vol quoted en % pasada a una función que espera bps (o viceversa), When la magnitud es incompatible (p. ej. σ_normal > 1000 bp o σ_lognormal < 0.5%), Then se rehúsa con `VolUnitsError` — la "trampa de unidades" del wiki, convertida en test.
- AC-1.5: Given una swaption, When se piden griegas, Then delta (en DV01 del swap subyacente), gamma, vega (por bp de vol normal) y theta se calculan por bump-and-reprice con el bump registrado en evidencia.

### US-2: Caps y floors
As a user, I want valuar caps/floors sobre Term SOFR 3M como suma de caplets/floorlets Black o Bachelier, so that cubra el caso de préstamo flotante del temario L3.
- AC-2.1: Given un cap y un floor al mismo strike, When se valúan, Then cap − floor = PV de un swap payer al strike (paridad) a 1e-8.
- AC-2.2: Given un caplet, When el strike → 0 con Black, Then el valor → PV del flujo flotante (límite).
- AC-2.3: Given un cap con un caplet cuyo periodo no tiene forward en la curva de tenor, When se valúa, Then se rehúsa con `MissingForwardError`.

### US-3: Cubo de volatilidad con SABR
As a quant, I want almacenar un cubo expiry×tenor×strike en vol normal y llenar huecos con SABR o shifted-SABR, so that pueda valuar strikes no cotizados con dinámica de smile estable.
- AC-3.1: Given un cubo con huecos (fixture), When se pide un punto no cotizado, Then el valor viene de SABR calibrado al smile de ese expiry×tenor y la evidencia dice qué puntos cotizados usó y los parámetros (α, β, ρ, ν).
- AC-3.2: Given SABR calibrado, When se compara la fórmula de Hagan (eq. 2.17, [[Stochastic, Local and Rough Volatility Models]]) contra los puntos cotizados, Then el RMSE < 0.5 bp normal en el fixture.
- AC-3.3: Given un smile con forward negativo o cercano a cero, When se usa SABR sin shift, Then se rehúsa con `ShiftRequiredError` y sugiere shifted-SABR.
- AC-3.4: Given el cubo, When se serializa, Then indica unidad (normal bps / lognormal %), convención de strike (absoluto / relativo al forward) y fecha.

### US-4: Modelos paramétricos de curva
As a user, I want ajustar Nelson-Siegel/Svensson y AFNS a la curva cero, y una curva escalonada entre fechas FOMC a futuros SR1/SR3 (Heitfield-Park), so that tenga alternativas al bootstrap para comparar y para término-rates.
- AC-4.1: Given una curva cero bootstrapeada, When se ajusta Nelson-Siegel, Then el RMSE en bp se reporta y en el fixture es < 3 bp; los parámetros (β₀, β₁, β₂, τ) van en evidencia.
- AC-4.2: Given SR1 y SR3 y fechas FOMC, When se ajusta la curva escalonada por mínimos cuadrados, Then los settlements implícitos re-precian los futuros a < 1 bp y la tasa a término 3M/6M implícita se reporta con la nota "sin ajuste de convexidad, válido < 1y" ([[SOFR Futures — Pricing, Convexity and Hedging Swaps]]).
- AC-4.3: Given una curva paramétrica, When se usa para valuar un swap, Then el payload marca `curve_kind="parametric"` y la diferencia en PV contra la curva bootstrapeada.
- AC-4.4: Given el repo `alanvaa06/Nelson_Siegel_Model`, When se implementa NS aquí, Then no se importa ese repo: se reimplementa con tests propios **[asumo que Alan prefiere no acoplar]**.

### US-5: Interpolación monotone-convex
As a quant, I want `interpolation="monotone_convex"` como opción, so that compare forwards suaves contra log-lineal.
- AC-5.1: Given los mismos instrumentos, When se bootstrapea con ambas interpolaciones, Then ambas re-precian los instrumentos a < 0.01 bp y el payload reporta la diferencia máxima de forward instantáneo entre nodos en bp.
- AC-5.2: Given monotone-convex, When los forwards de entrada son positivos, Then los forwards interpolados son positivos (propiedad de Hagan-West).

### US-6: Servidor MCP
As an agent-facing pipeline, I want `rateng-mcp` con herramientas `bootstrap`, `price`, `hedge`, `describe`, `list_instruments`, so that un agente llame el motor como herramienta sin reimplementar serialización.
- AC-6.1: Given el extra `[mcp]` instalado (Python ≥ 3.10), When se lanza `rateng-mcp`, Then las cinco herramientas devuelven exactamente el payload del comando `--json` equivalente (test de igualdad byte a byte sobre fixtures).
- AC-6.2: Given un error anticipado (curva imposible, fixing faltante), When ocurre vía MCP, Then se levanta `ToolError` con el mensaje de la excepción nombrada; nunca un wrapper mudo.
- AC-6.3: Given Python 3.9, When se intenta instalar `[mcp]`, Then el mensaje de error nombra la versión mínima (patrón optengine).
- AC-6.4: Given cualquier herramienta MCP, When se ejecuta, Then no escribe archivos, no toca la red y no invoca ningún modelo.

## Constraints
- Todo lo de PRD-001 sigue vigente; ningún número de v1 cambia sin entrada en CHANGELOG bajo **Changed** con lo que se mueve.
- σ implícita permitida como fuente de convexidad solo con `warnings: ["atm_only_calibration_overstates_10_25pct"]` en evidencia.
- SABR: implementación propia de la fórmula de Hagan; sin dependencias nuevas en core.
- MCP: `mcp>=2.0,<3` como extra; el servidor es un módulo delgado sobre `reporting.payloads`.

## Definition of Done
- [ ] Todos los AC verdes; property tests para paridades (payer/receiver, cap/floor).
- [ ] `docs/RESEARCH.md` extendido con [[Term Structure Models for Swaps and Swaptions]] y [[Stochastic, Local and Rough Volatility Models]].
- [ ] `AGENTS.md`: sección "las cosas que muerden" con la trampa de unidades de vol.
- [ ] CI añade job con extra `[mcp]` y asserta que `rateng-mcp` está en PATH.
- [ ] Tag `v0.2.0`.

## Preguntas abiertas
1) ¿SABR completo (α, β, ρ, ν con β libre) o β fijo?
   a) β fijo en 0.5 (o 0 para normal) y calibrar α, ρ, ν — **Recommended**, práctica de mercado y calibración estable
   b) β libre
2) ¿Svensson además de Nelson-Siegel?
   a) Solo NS en v2 — **Recommended**, menor scope; Svensson si NS no ajusta el fixture
   b) Ambos
