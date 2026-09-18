# PRD-003: v3 — curva MXN, FX forwards USD/MXN y pricer de coberturas

> Escrito 2026-09-16. Depende de PRD-001 y PRD-002 (v0.2.0 merged a main 2026-09-18). Formato forge-master. **Estado: research previo ejecutado 2026-09-18 (`docs/forge/research/003-mxn-conventions.md`); preguntas abiertas cerradas; en construcción.** Supuestos marcados **[asumo]**.

## Goal
Publicar `finport-ratesengine` v0.3: segunda moneda (MXN) con curva TIIE, FX forwards USD/MXN vía paridad cubierta más basis cross-currency, opciones FX vanilla (Garman-Kohlhagen) con smile vanna-volga desde ATM/RR/BF y convenciones de delta explícitas, y un comparador determinista de estructuras de cobertura (forward, over/under-hedge, put, put OTM, collar, put spread, seagull) para una exposición de transacción — el marco de CFA L3 Reading 19 y Derivatives. Sin agentes: el "memo" es una tabla determinista, no texto generado.

## Non-Goals
- Cualquier LLM que redacte recomendaciones; el comparador devuelve números y una tabla de trade-offs, no consejo.
- Exposición de traslación y económica (solo transacción).
- Opciones FX exóticas (barreras, digitales, target redemption).
- Estrategias activas de divisas (carry, momentum, fundamentales).
- Monedas distintas de USD y MXN.
- Superficies de vol FX en vivo: v3 toma ATM/RR/BF como input del usuario o fixture.
- Overlay de portafolio multi-moneda (hedge ratio dinámico sobre un portafolio es v4 si acaso).

## User Stories

### US-1: Curva MXN
As a user, I want construir la curva de descuento MXN desde TIIE de Fondeo (o TIIE 28 histórica) y swaps TIIE, so that descuente flujos en pesos con las convenciones locales.
- AC-1.1: Given fixings de TIIE de Fondeo (Banxico SIE) y swaps TIIE par, When se bootstrapea, Then cada instrumento re-precia a < 0.01 bp con day count ACT/360 y periodos de 28 días **[asumo, verificar en research previo]**.
- AC-1.2: Given el calendario mexicano (Banxico) de 2026, When se pide el día hábil siguiente a un feriado, Then coincide con la lista publicada. `[manual-check]` para la fuente.
- AC-1.3: Given TIIE 28 y TIIE de Fondeo para una misma fecha, When se construye una curva con una u otra, Then el payload marca `benchmark="TIIE28"|"TIIE_FONDEO"` y la diferencia en bp de la curva cero; nunca se mezclan sin marca.
- AC-1.4: Given una convención MXN no verificada en el research previo, When se usa, Then la evidencia arrastra `unresolved_conventions` con el nombre; si `strict=True`, se rehúsa.

### US-2: FX forward con paridad cubierta y basis
As a treasurer, I want el forward USD/MXN a T días desde spot, curva USD, curva MXN y basis cross-currency, so that sepa cuánto cuesta fijar el tipo de cambio y cuánto de eso es basis.
- AC-2.1: Given spot, DF USD y DF MXN, When se calcula el forward sin basis, Then $F = S \cdot P_{USD}(T)/P_{MXN}(T)$ (CIP, [[CFA Economics — Currency Exchange Rates]]) y los puntos forward se reportan en pips con la convención de cotización declarada.
- AC-2.2: Given un basis cross-currency en bp, When se incorpora, Then el forward cambia y el payload descompone: `forward = cip_forward + basis_component`, con cita a [[Forwards, Multi-Curve and Swaptions (Post-LIBOR)]] ("CIP roto desde 2007").
- AC-2.3: Given un forward de mercado (fixture), When se invierte, Then se obtiene el basis implícito en bp y la evidencia dice qué curvas se usaron.
- AC-2.4: Given un basis fuera de rango plausible (p. ej. |basis| > 500 bp), When se recibe, Then se rehúsa con `ImplausibleInputError` — no se acepta un dato que el wiki marca como "no citable" sin confirmación.

### US-3: Opciones FX con Garman-Kohlhagen y smile vanna-volga
As a quant, I want valuar puts/calls USD/MXN con dos tasas y construir el smile desde ATM, RR25, BF25 (y 10Δ) con vanna-volga, so that cotice cualquier strike con la convención de delta correcta.
- AC-3.1: Given S, K, T, r_USD, r_MXN, σ, When se valúa con Garman-Kohlhagen, Then cumple put-call parity $C - P = S e^{-r_f T} - K e^{-r_d T}$ a 1e-10.
- AC-3.2: Given ATM/RR/BF, When se construye el smile vanna-volga, Then reproduce exactamente los tres puntos cotizados y el payload muestra la vol en 10Δ, 25Δ y ATM en ambas alas.
- AC-3.3: Given una vol por delta, When se convierte a strike, Then el payload declara `delta_convention ∈ {spot, forward} × {premium_adjusted, unadjusted}` y el strike cambia entre convenciones; test: para USD/MXN con premium en USD el strike difiere de la convención no ajustada (la "trampa" del wiki convertida en test) **[asumo convención por defecto para USD/MXN; verificar en research]**.
- AC-3.4: Given una convención de delta no declarada, When se pide un strike desde delta, Then se rehúsa con `DeltaConventionError`; nunca se asume.
- AC-3.5: Given una opción, When se piden griegas, Then delta (en ambas convenciones), gamma, vega, vanna y volga por bump-and-reprice.

### US-4: Comparador de estructuras de cobertura (CFA L3)
As a treasurer with a USD payable in 90 days, I want comparar forward, over/under-hedge, put ATM, put OTM, collar, put spread y seagull con costo inicial, piso, techo y payoff bajo escenarios, so that decida con la tabla de trade-offs del temario y no con una recomendación.
- AC-4.1: Given una exposición (monto, moneda, fecha), When se pide `compare_structures`, Then devuelve una tabla con, por estructura: prima neta, tipo de cambio efectivo peor caso, mejor caso, participación en upside, y payoff en una malla de spots a vencimiento.
- AC-4.2: Given las estructuras, When se ordenan por prima, Then se cumple **seagull ≤ put spread ≤ collar ≤ put OTM ≤ put ATM** en costo inicial para strikes coherentes (fixture), y el collar "cero costo" tiene prima neta 0 ± 1e-6 con el call strike resuelto.
- AC-4.3: Given un hedge ratio h ∈ [0, 1], When se aplica a la estructura forward, Then el payoff es la combinación lineal correcta y el payload reporta la varianza residual $σ²(RDC)$ con la descomposición $σ²(RFC) + σ²(RFX) + 2ρσσ$ ([[CFA L3 Asset Allocation — Constraints, Currency, and Benchmarks]]).
- AC-4.4: Given la tabla, When se serializa, Then no contiene texto de recomendación; solo números, nombres de estructura y el marco de trade-off (costo inicial ↑ → protección ↑, costo de oportunidad ↓) como campos etiquetados.
- AC-4.5: Given un usuario que pide "la mejor estructura", When se llama la API, Then no existe tal función; `describe` explica que el motor compara y no recomienda (test de superficie: ningún nombre público contiene `recommend`).

### US-5: Programa de cobertura como configuración
As a user, I want declarar un programa (hedge ratio objetivo, banda de discreción, frecuencia de rebalanceo, instrumentos permitidos) en YAML y que el motor valide la cobertura contra él, so that la política y el cálculo vivan separados.
- AC-5.1: Given un YAML con `target_hedge_ratio`, `discretion_band`, `rebalance_frequency`, `allowed_instruments`, When se carga, Then una key desconocida lanza `ConfigurationError` nombrándola (patrón optengine).
- AC-5.2: Given una cobertura propuesta fuera de la banda, When se audita, Then el payload reporta la violación con `severity` y `suggestion`; con `strict=True` se rehúsa.

## Decisiones cerradas (2026-09-18)

| # | Pregunta | Elegido | Razón |
|---|---|---|---|
| 1 | TIIE 28 histórica además de TIIE de Fondeo | **Ambas, con marca explícita** (1a) | AC-1.3 ya exige que el payload las distinga; soportar una sola volvería ese AC vacío |
| 2 | Vanna-volga o SABR para el smile FX | **Vanna-volga** (2a) | Reproduce los tres puntos cotizados exactamente — medido a `0.00e+00` en el prototipo del research. SABR no tiene esa propiedad por construcción: ajusta una sonrisa, no interpola pilares |
| 3 | Moneda en el tipo, o implícita | **Explícita, aditiva, default USD** | Hallazgo del research: el motor no tiene concepto de moneda en ninguna parte. Con una segunda moneda, nada impide descontar flujos MXN en la curva USD ni sumar un PV en dólares con uno en pesos; ambas cosas devuelven un número y la cadena de evidencia no registra nada. Es la misma clase de error que las unidades de vol en v2 y tiene el mismo arreglo: el tipo lleva el hecho. Default USD mantiene funcionando toda llamada de v1 y v2, así que el cambio es aditivo y no viola la Constraint de que ninguna curva USD cambia |

La decisión 3 no estaba en el PRD. Sale del research y se registra aquí
porque cambia firmas públicas, aunque de forma compatible, y porque va
primero: reajustarla después de cuatro user stories de código MXN es
estrictamente más caro.

## Resultado del research previo (2026-09-18)

Ejecutado. `docs/forge/research/003-mxn-conventions.md` tiene el detalle;
el resumen es que **la maquinaria de v3 es construible y las convenciones
mexicanas no son verificables desde aquí**.

Banxico, ISDA, CME, BIS y Wikipedia devuelven 403 en el proxy de egress por
política de la organización — el mismo límite que dejó saltados los goldens
CME de v1. Lo único sustantivo alcanzable es QuantLib en GitHub, que es
mejor fuente de lo que suena porque su código se puede **leer**, no sólo
citar.

Lo que eso deja:

- **Calendario:** QuantLib tiene las trece reglas del calendario mexicano,
  pero su implementación se llama `BmvImpl` y se identifica como "Mexican
  stock exchange". Es el calendario de la **BMV, no el bancario de
  Banxico** que pide AC-1.2. Son listas distintas y no tienen por qué
  coincidir. Se implementa con su nombre real y el diff contra Banxico
  queda `[manual-check]`, igual que la regla del sábado de SIFMA en v1.
- **TIIE: nada.** Cero resultados en todo QuantLib. Day count, periodo de
  28 días, la distinción Fondeo/28 y los IDs de serie de SIE no quedan
  establecidos por nada alcanzable.
- **Todo lo demás de US-2, US-3, US-4 y US-5** es forma cerrada o
  aritmética sobre inputs que da el usuario, y se construye sin convención
  mexicana alguna. El prototipo de vanna-volga del research no necesitó
  ninguna: **declaró** una convención de delta, que es justo lo que AC-3.3
  y AC-3.4 exigen. El hueco está en el *default*, no en la maquinaria — así
  que no habrá default y `DeltaConventionError` es la respuesta.

AC-1.4 es la costura que el propio PRD construyó para esto, y la fase se
arma a lo largo de ella: la evidencia arrastra `unresolved_conventions` con
el nombre de cada convención sin verificar, y `strict=True` se rehúsa.

## Constraints
- **Research previo obligatorio antes de plan-design:** ejecutado 2026-09-18, ver arriba. convenciones TIIE 28 / TIIE de Fondeo (day count, periodo, fuente Banxico SIE), calendario MX, convención de delta y premium USD/MXN, fuente de ATM/RR/BF y del basis. El vault no lo cubre (gap declarado 2026-09-16); el resultado va a `raw/` y se compila antes de este PRD.
- Todo lo de PRD-001 y PRD-002 vigente; ninguna curva USD cambia.
- Sin dependencias nuevas en core; Banxico SIE como proveedor en extra `data` (requiere token gratuito del usuario, nunca en el repo).
- Salida de consola ASCII (Windows).
- Los ejemplos del README usan una exposición ficticia; ningún dato de cliente.

## Definition of Done
- [ ] Todos los AC verdes; property tests para put-call parity, paridad forward y orden de primas.
- [ ] `[manual-check]` AC-1.2 verificado contra Banxico.
- [ ] `docs/RESEARCH.md` extendido con el research MXN compilado y con [[CFA L3 Derivatives — Forwards, Futures, and Options]] y Reading 19.
- [ ] `AGENTS.md`: sección sobre convenciones de delta y por qué no hay `recommend`.
- [ ] Tag `v0.3.0`.

## Preguntas abiertas

Cerradas 2026-09-18; ver "Decisiones cerradas" arriba. Se conservan aquí las
opciones tal como se plantearon.

1) ¿TIIE 28 histórica además de TIIE de Fondeo? → **(a)**
   a) Ambas, con marca explícita — **Recommended**, permite backtests previos a la transición
   b) Solo TIIE de Fondeo
2) ¿Vanna-volga o SABR para el smile FX? → **(a)**
   a) Vanna-volga — **Recommended**, estándar FX y reproduce los 3 puntos exactamente ([[Forwards, Multi-Curve and Swaptions (Post-LIBOR)]])
   b) Reusar SABR de v2 — menos código, dinámica de smile distinta a la del mercado FX
