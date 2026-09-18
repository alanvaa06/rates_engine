# PRD-002: v2 — swaptions, caps/floors, cubo de vol, modelos de curva y MCP

> Escrito 2026-09-16. Depende de PRD-001 completo (v0.1.0 merged a main 2026-09-18). Formato forge-master. **Estado: construido 2026-09-18; los 22 AC verdes.** Supuestos marcados **[asumo]**.

## Goal
Publicar `finport-ratesengine` v0.2: añadir opciones de tasa vanilla (swaptions payer/receiver, caps/floors) con Black lognormal y Bachelier normal en bps, un cubo de vol expiry×tenor×strike con SABR/shifted-SABR como interpolador, modelos de curva paramétricos (Nelson-Siegel y curva escalonada FOMC de Heitfield-Park) como alternativa al bootstrap, interpolación monotone-convex, y un servidor MCP que expone los mismos payloads que `--json`. Sigue sin IA en runtime; el MCP es transporte.

## Non-Goals
- **AFNS (Christensen-Diebold-Rudebusch).** Lo mencionaba el Goal original y ningún AC lo probaba — desajuste corregido 2026-09-18. El término de ajuste libre de arbitraje tiene forma cerrada, pero sus parámetros salen de estimar un modelo de estado-espacio con Kalman sobre un panel histórico, que es una pieza de investigación, no un ajuste de curva. Implementarlo a medias daría un modelo que se llama libre de arbitraje sin serlo. Nelson-Siegel sí entra; AFNS queda como investigación posterior.
- **Svensson** (pregunta abierta 2): solo Nelson-Siegel en v2. El código deja el hueco para el cuarto factor sin abrirlo.
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
- AC-1.2 **(reformulado 2026-09-18)**: Given σ lognormal, When se valúa con Black, Then reproduce un fixture de ≥ 20 casos construido desde la definición, no desde este código, y satisface las identidades cerradas que ningún error de implementación sobrevive: paridad payer/receiver, límite σ→0 al valor intrínseco, límite T→0 al intrínseco, monotonía en σ, y convergencia a Bachelier cuando σ_lognormal·F → σ_normal con σ pequeña.

  El AC original decía "coincide con el pricer del temario". No hay tal pricer alcanzable desde aquí, y un fixture generado por este mismo código probaría que el código coincide consigo mismo. Lo que sí prueba algo es una reimplementación independiente más las identidades: la reimplementación atrapa un error de fórmula, las identidades atrapan un error que ambas implementaciones compartan.
- AC-1.3: Given un precio de swaption, When se invierte a vol normal y a vol lognormal, Then ambas inversiones reproducen el precio a 1e-8 y la evidencia reporta ambas con la regla ATM `σ_normal ≈ σ_lognormal × F` como chequeo ([[Volatility, Greeks and Option Strategy Practice (2026)]]).
- AC-1.4 **(umbral corregido 2026-09-18)**: Given una vol pasada con la unidad equivocada, When se construye, Then se rehúsa con `VolUnitsError`. La primera línea de defensa es el tipo: `Volatility` lleva su unidad y **no** convierte entre normal y lognormal, porque no hay conversión — solo una equivalencia at-the-money. La banda de magnitud cubre el resto: σ_normal fuera de [0.1, 1000] bp, σ_lognormal fuera de [5%, 500%].

  El piso lognormal sube de 0.5% a **5%**. Con 0.5% el AC no atrapa el error que describe: las vols normales de swaption viven en 60-150 bp, que como decimales son 0.006-0.015, todas por encima de 0.005. La población entera de "normal pasada como lognormal" pasaba el filtro. Las lognormales viven en 15-60%; 5% cae en el hueco entre ambas poblaciones y las separa. `Volatility.unchecked` queda para el caso genuinamente extremo.
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
- AC-6.3 **(corregido 2026-09-18)**: Given el SDK de MCP ausente, When se invoca `rateng-mcp` o se importa el servidor, Then el error nombra el extra que lo instala (`pip install "finport-ratesengine[mcp]"`), nunca un `ModuleNotFoundError` desnudo.

  El AC original decía "Given Python 3.9". Contradice la decisión D1 de PRD-001, que subió el piso a 3.11: en 3.9 no se instala el paquete, así que no hay nada que probar sobre su extra.
- AC-6.4: Given cualquier herramienta MCP, When se ejecuta, Then no escribe archivos, no toca la red y no invoca ningún modelo.

## Decisiones cerradas (2026-09-18)

| # | Pregunta | Elegido | Razón |
|---|---|---|---|
| 1 | SABR: β libre o fijo | **β fijo, configurable, default 0.5**; se calibran α, ρ, ν | Con un solo smile expiry×tenor, β y ρ son casi inidentificables: ambos controlan el backbone y la asimetría, así que la calibración conjunta está mal condicionada y devuelve parámetros que saltan entre recalibraciones sin que el ajuste mejore. β se elige a priori por convención de mercado (0 normal, 0.5 CIR, 1 lognormal) |
| 2 | Svensson además de NS | **Solo Nelson-Siegel** | Menor superficie; el fixture no necesita el cuarto factor |
| 3 | AFNS | **Fuera** (ver Non-Goals) | Ningún AC lo probaba y requiere estimación de estado-espacio |
| 4 | Unidades de vol | **Tipo explícito `Volatility(value, units)`**, no float desnudo | La "trampa de unidades" de AC-1.4 existe porque las vols viajan como floats. Un tipo que lleva su unidad la elimina por construcción; el chequeo de magnitud queda para el caso restante, unidad correcta y número absurdo |

## Constraints
- Todo lo de PRD-001 sigue vigente; ningún número de v1 cambia sin entrada en CHANGELOG bajo **Changed** con lo que se mueve.
- σ implícita permitida como fuente de convexidad solo con `warnings: ["atm_only_calibration_overstates_10_25pct"]` en evidencia.
- SABR: implementación propia de la fórmula de Hagan; sin dependencias nuevas en core.
- MCP: `mcp>=2.0,<3` como extra; el servidor es un módulo delgado sobre `reporting.payloads`.

## Definition of Done
- [x] Todos los AC verdes; property tests para paridades (payer/receiver, cap/floor).
- [x] `docs/RESEARCH.md` extendido con [[Term Structure Models for Swaps and Swaptions]] y [[Stochastic, Local and Rough Volatility Models]].
- [x] `AGENTS.md`: sección "las cosas que muerden" con la trampa de unidades de vol.
- [x] CI añade job con extra `[mcp]` y asserta que `rateng-mcp` está en PATH.
- [ ] Tag `v0.2.0` (pendiente de aprobación de Alan, como v0.1.0).

## Estado de implementación (2026-09-18)

v0.2.0 construido. `pytest -q`: **971 pasan, 5 saltan** con el SDK de MCP
instalado; 968/8 sin él. `ruff check .` limpio.
`mypy src/rates_engine` limpio en 48 archivos, allowlist vacía.
`python scripts/audit_acceptance.py 002`: **22 AC, 0 sin cubrir, 0 parciales**.

Los 5 que saltan son los goldens CME de PRD-001, sin cambio. Los 3 del
cableado del transporte MCP saltan solo donde el SDK falta; el índice de
PyPI se recuperó a mitad de sesión y ahora corren, igual que en el job `mcp`
de CI, que falla si algo salta allí — un job que instala el SDK y reporta
verde sin haber probado nada sería peor que no tenerlo.

### Lo que CI encontró y la sesión no

El job `mcp` falló en el primer PR. `mcp` 2.x renombró `FastMCP` a
`MCPServer` (`mcp.server.mcpserver`), y el código estaba escrito contra la
API 1.x. El PRD ya especificaba `mcp>=2.0,<3` en Constraints: el desajuste
era del código, no del PRD. Imposible de ver en la sesión mientras PyPI
estuvo caído, y visible en el primer job que instaló el extra — que es
exactamente para lo que se añadió ese job.

Dos consecuencias, ambas peores que el rename:

**El mensaje mentía.** El `except ImportError` envolvía el fallo en
`MissingDependencyError`: "the MCP server needs the SDK", sobre un SDK que
*sí* estaba instalado. Mandar a alguien a reinstalar lo que ya tiene es la
clase de negativa que este paquete existe para no dar. Ahora
`build_server()` separa los dos casos: `import mcp` falla → ausente;
`import mcp.server.mcpserver` falla → `IncompatibleDependencyError`, que
nombra el rango y qué se movió. Subclase de `MissingDependencyError`, así
que un solo `except` sigue cubriendo "el extra no sirve".

**AC-6.2 estaba roto en el transporte.** El wrapper levantaba `RuntimeError`,
y el SDK 2.x aplana cualquier excepción que no sea su propio `ToolError` a
"Error executing tool <name>" — el mensaje se pierde. Es literalmente el
wrapper mudo que el AC prohíbe, y ningún test sin SDK podía verlo porque el
aplanamiento ocurre dentro del SDK. Ahora se levanta `ToolError` (que es lo
que el AC dice, palabra por palabra) y hay dos tests que verifican que
`CurveArbitrageError` y su instrumento `SR3-4` llegan al llamador.

La lección no es sobre MCP. Es que los cuatro AC de US-6 se probaban sin el
SDK por diseño, y esa cobertura es real para tres de ellos y **no** para
AC-6.2, cuyo contenido entero es lo que el transporte hace con la excepción.
Probar todo lo que no depende del tercero ausente sigue siendo correcto;
creer que eso cubre un AC cuyo sujeto *es* el tercero, no.

### Lo que cambió respecto al PRD durante la construcción

**AC-6.3 tiene un mecanismo que el AC no pedía.** El AC exige que el error
nombre el extra. Para que ese error pueda levantarse, el módulo tiene que
importar sin el SDK, así que las cinco herramientas son funciones ordinarias
sobre `reporting.payloads` y el SDK se importa dentro de `build_server()`.
El efecto secundario es que AC-6.1, AC-6.2 y AC-6.4 se prueban sin el SDK.

**AC-4.3 no tenía dónde vivir.** Pedía que el payload marcara
`curve_kind="parametric"` y reportara la diferencia de PV contra la curva
bootstrapeada, y no existía función que valuara sobre las dos. Se añadió
`pricing.price_on_parametric`, que toma el ajuste y lee de él el nombre del
modelo en vez de aceptar una cadena: una etiqueta que el llamador escribe
puede desalinearse de la curva que realmente usó.

**Una excepción nueva que el PRD no anticipaba.** `ConfigurationError`. Las
herramientas MCP reciben el config como argumento opcional, de modo que
llamar a `bootstrap` sin config salía como el primer `KeyError` que apareciera.
Por la CLI es inalcanzable (argparse exige `--config`), pero la regla del
repo es que toda negativa es una excepción nombrada.

### Dos errores encontrados por los tests, no por lectura

**Signo en la serie de `_z_over_x` (SABR).** La expansión da
`x(z) = z + ρz²/2`, luego `z/x(z) = 1/(1 + ρz/2) ≈ 1 − ρz/2`. El código tenía
`1 + ρz/2`. El error era menor a 1e-7 en vol implícita y solo cerca del
dinero, así que ningún test de nivel lo habría visto: lo encontró un test de
continuidad en la frontera de la rama, donde el salto cayó de 8e-8 a 1e-9.

**División por cero en la región 4 de Hagan-West.** Cuando `g0` o `g1` es
exactamente cero, `eta = g1/(g0+g1)` colapsa sobre un extremo. El límite de
la forma de región 4 ahí es `g ≡ 0` en todo el intervalo, cuya integral
coincide con la de la cuadrática simple, así que ningún factor de descuento
de nodo cambia.

### Tres de mis propios tests estaban mal especificados

No eran errores del código. Uno confundía backbone con skew (con β < 1 el
backbone ya inclina la sonrisa aunque ρ = 0); otro ignoraba la corrección en
T al afirmar "sin vol de vol"; el tercero pedía una tolerancia más estrecha
que la pendiente de la propia sonrisa. Los tres se reescribieron para aislar
lo que dicen probar. Vale la pena anotarlo porque la tentación en los tres
casos era aflojar la tolerancia.

### Fixtures nuevos, y por qué están construidos así

No hay fuente libre de cotizaciones de swaption, curvas cero o settlements
de futuros. Los cuatro fixtures nuevos son construidos y su procedencia lo
dice. Lo que importa es *cómo*: cada uno sale de una forma que el modelo
ajustado no puede reproducir.

| Fixture | Generado por | Por qué esa forma |
|---|---|---|
| `swaption_vol_cube.csv` | sonrisa cuadrática en moneyness | SABR no es cuadrática, así que el RMSE de AC-3.2 mide la expansión, no el solver |
| `zero_curve.csv` | curva de Svensson | NS es Svensson sin el segundo factor, así que no la alcanza; el RMSE de AC-4.1 mide el modelo |
| `fomc_futures_strip.csv` | trayectoria escalonada conocida, redondeada al tick | ninguna trayectoria re-precia la tira redondeada exactamente; el residual de AC-4.2 es real |
| `fomc_meetings.csv` | la trayectoria que generó la tira | permite probar recuperación, no solo ajuste |

Un cubo generado por SABR habría convertido AC-3.2 en "el solver coincide
consigo mismo", que es el error que los goldens saltados de v1 existen para
no cometer.

### Riesgo ambiental que se materializó

El SDK de MCP sigue sin instalarse: `pip install mcp` agota el tiempo contra
PyPI en este entorno. La estructura descrita arriba es la respuesta, no una
excusa: los cuatro AC de US-6 se prueban sin el SDK — la igualdad byte a byte
con `--json`, el mapeo de errores, el mensaje del extra ausente y la ausencia
de efectos secundarios — y lo único que queda tras el `importorskip` son tres
tests del cableado del transporte, que corren en el job `mcp` de CI.

## Riesgo ambiental conocido

El SDK de MCP no es instalable en el entorno de construcción: el índice de PyPI se degradó a mitad de sesión y `pip download mcp` agota el tiempo igual que `pip download numpy`, que sí se había instalado antes. No es que el paquete no exista.

Esto **no** exime US-6, lo dirige: el servidor se estructura como funciones puras sobre `reporting.payloads` más un `main()` delgado que el SDK envuelve. Así AC-6.1 (igualdad con el payload de `--json`), AC-6.2 (mapeo de errores) y AC-6.4 (sin red, sin archivos, sin modelo) se prueban sin el SDK, y solo el cableado del transporte queda tras un `importorskip`. Es la misma disciplina que los goldens CME de v1: se prueba todo lo que no depende del tercero ausente, y lo que sí depende se salta nombrando lo que falta.
