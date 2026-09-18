# PRD-003: v3 — curva MXN, FX forwards USD/MXN y pricer de coberturas

> Escrito 2026-09-16. Depende de PRD-001 y PRD-002 (v0.2.0 merged a main 2026-09-18). Formato forge-master. **Estado: construido 2026-09-18; los 20 AC verdes.** Supuestos marcados **[asumo]**.

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
- [x] Todos los AC verdes; property tests para put-call parity, paridad forward y orden de primas.
- [ ] `[manual-check]` AC-1.2 verificado contra Banxico — **pendiente, bloqueado por egress** (403). `tests/fixtures/bmv_holidays.csv` está listo para diffear.
- [x] `docs/RESEARCH.md` extendido con el research MXN compilado y con [[CFA L3 Derivatives — Forwards, Futures, and Options]] y Reading 19.
- [x] `AGENTS.md`: sección sobre convenciones de delta y por qué no hay `recommend`.
- [ ] Tag `v0.3.0` (pendiente de aprobación de Alan, como v0.1.0 y v0.2.0).

## Estado de implementación (2026-09-18)

v0.3.0 construido. `pytest -q`: **1442 pasan, 5 saltan** (los goldens CME de
v1, sin cambio). `ruff check .` limpio. `mypy src/rates_engine` limpio en 59
archivos, allowlist vacía. `python scripts/audit_acceptance.py 003`:
**20 AC, 0 sin cubrir, 0 parciales**.

### La decisión que no estaba en el PRD, y por qué fue primero

`grep -rn "currency" src/` devolvía dos resultados, ambos prosa en
docstrings. El motor no tenía concepto de moneda porque nunca había habido
más de una. Con MXN, descontar flujos en pesos sobre la curva USD devuelve
un número, sumar un PV en dólares con uno en pesos devuelve un número, y la
cadena de evidencia —que es el argumento entero del paquete— no registra
ninguna de las dos cosas.

Es la misma clase de error que las unidades de vol en v2 y tiene el mismo
arreglo: el tipo lleva el hecho. Default USD, así que las 1276 pruebas de v1
y v2 corren **sin modificarse** — que es la única forma de probar que el
cambio es aditivo. Si alguna hubiera necesitado cambiar, no lo habría sido.

Un bug propio salió de ahí: `DiscountCurve.shifted` y `.with_node`
reconstruían la curva y **perdían la moneda**. El bootstrap pasa por
`with_node` una vez por instrumento, así que toda curva bootstrapeada salía
USD. Una moneda que un bump tira es peor que ninguna, porque la negativa
deja de dispararse justo donde la curva pasó por más maquinaria.

### El bug de v1 que encontró el segundo calendario

`holidays(year)` devuelve las fechas en que se **observan** los feriados de
ese año, y una regla de fin de semana puede sacar uno de su propio año: el
1 de enero de 2022 cayó sábado, se observa el viernes 31 de diciembre de
2021, y esa fecha pertenece a `holidays(2022)`. `is_business_day` sólo
miraba `holidays(day.year)`, así que reportaba ese viernes —y el 31 de
diciembre de 2027, y cada año equivalente— como día hábil. Cualquier cosa
que rodara o contara a través de esas fechas quedaba corrida un día.
Presente en 0.1.0 y 0.2.0. Ningún test lo atrapó porque todos los fixtures
de la suite empiezan en enero; hizo falta un calendario **sin** regla de
observancia para que la diferencia se viera.

### Lo que el research gate cambió en la forma del código

Nada de MXN es verificable desde aquí. La respuesta no fue adivinar ni
parar, sino la costura que AC-1.4 ya preveía: `UNRESOLVED_MXN` es una tupla
de `(nombre, por qué)`, cada entrada se vuelve una `Degradation` con calidad
`ASSUMED` en todo resultado en pesos, `worst_quality` llega a `ASSUMED`,
cualquier precio encima lo hereda, y `strict_conventions=True` lo convierte
en negativa antes de calcular. Un test lo prueba como dato y no como código:
se vacía la tupla con monkeypatch y `strict_conventions` deja de rehusar sin
tocar una línea.

Lo mismo con el delta: el research no estableció qué convención cotiza
USD/MXN, así que **no hay default** y `DeltaConventionError` explica el
motivo en el mensaje. Y lo mismo con el calendario: QuantLib da las trece
reglas, pero su implementación se llama `BmvImpl` — es el calendario de la
**BMV**, no el bancario de Banxico que pide AC-1.2. Se llama BMV, la
diferencia es una de las entradas de `UNRESOLVED_MXN`, y el fixture está
listo para que alguien lo diffee.

### Un hallazgo que no se ajustó para que saliera bonito

AC-4.2 pide el orden **seagull ≤ put spread ≤ collar ≤ put OTM ≤ put ATM**.
Se cumple exactamente para el ejemplo del propio PRD, un payable con strikes
coherentes. **No** se cumple para un receivable con los mismos offsets: el
collar y el spread se intercambian, porque el diferencial de tasas pone el
forward muy por encima del spot y las alas no son simétricas alrededor. Un
orden que sólo se cumple después de ajustar los offsets para que se cumpla no
es una propiedad de las estructuras. El test lo asiente donde se cumple y
asiente que falla donde falla; lo que **sí** es universal —protección ATM
completa es lo más caro, el seagull lo más barato— se prueba en ambas
direcciones.

### Lo que falta, y cuánto cuesta

Una persona con navegador y media hora: las páginas de SIE para TIIE 28 y
TIIE de Fondeo (day count, regla de publicación, ID de serie), la lista del
calendario bancario de Banxico 2026, y una hoja de convenciones de cualquier
broker de USD/MXN para la base de delta. Con eso `UNRESOLVED_MXN` se vacía,
AC-1.1 pasa de "marcado" a "verificado", el calendario puede renombrarse o
corregirse, y el default de delta deja de ser una negativa. Nada de eso
requiere cambiar código.

## Preguntas abiertas

Cerradas 2026-09-18; ver "Decisiones cerradas" arriba. Se conservan aquí las
opciones tal como se plantearon.

1) ¿TIIE 28 histórica además de TIIE de Fondeo? → **(a)**
   a) Ambas, con marca explícita — **Recommended**, permite backtests previos a la transición
   b) Solo TIIE de Fondeo
2) ¿Vanna-volga o SABR para el smile FX? → **(a)**
   a) Vanna-volga — **Recommended**, estándar FX y reproduce los 3 puntos exactamente ([[Forwards, Multi-Curve and Swaptions (Post-LIBOR)]])
   b) Reusar SABR de v2 — menos código, dinámica de smile distinta a la del mercado FX
