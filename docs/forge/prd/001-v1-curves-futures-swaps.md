# PRD-001: v1 — curvas SOFR, futuros, swaps y cobertura

> Escrito 2026-09-16 desde el diseño `2026-09-16-rates-engine-design.md` y el wiki compilado el mismo día. Formato forge-master. **Estado: preguntas abiertas cerradas 2026-09-16 (Alan: A1, B2, C1, D1); listo para `plan-design`.** Supuestos míos marcados **[asumo]**; decisiones cerradas al final.

## Goal
Publicar `finport-ratesengine` v0.1: librería Python determinista que construye curvas SOFR (descuento, cero, par, forward) desde datos gratuitos, valúa futuros SOFR con ajuste de convexidad, valúa OIS/IRS/FRA con descuento a la tasa de colateral, y cubre un swap con una tira de futuros — devolviendo con cada número la evidencia sobre la que descansa. Cero IA en runtime.

## Non-Goals
- Opciones de tasa de cualquier tipo (swaptions, caps, floors) → v2.
- Cualquier moneda distinta de USD; nada de TIIE ni FX → v3.
- CVA/FVA/KVA/MVA, márgenes de CCP, SA-CCR. Precio limpio.
- Basis swaps como producto negociable (solo como instrumento de calibración en dual-curva, y en v1 solo con inputs sintéticos — ver US-7).
- Fuentes de par OIS licenciadas (ICE Swap Rate, CME Term SOFR comercial): fuera de v1 por costo/redistribución. El largo plazo usa proxy Treasury opt-in (decisión B2).
- Pronóstico de tasas; prima de riesgo; expectativas de política.
- Vol implícita como fuente de σ (no hay opciones en v1).
- Macaulay y modified duration, y con ellas `FixedRateBond`: requieren un rendimiento único y por tanto un instrumento de precio ≠ 0, que v1 no tiene → v1.1. v1 sí cubre *effective* duration/convexity y las medidas monetarias (US-10).
- Agregación de portafolio, atribución, backtesting.
- UI gráfica, servidor MCP (→ v2), notebooks como parte del paquete.
- Calendarios distintos de SIFMA US.
- Interpolación distinta de log-lineal en DF (monotone-convex → v2).

## User Stories

### US-1: Convenciones explícitas
As a quant, I want day counts, calendarios, roll y fechas IMM como tipos explícitos, so that ningún cálculo dependa de un default silencioso.
- AC-1.1: Given `DayCount.ACT_360`, When se calcula la fracción entre 2026-01-15 y 2026-04-15, Then el resultado es 90/360 exacto; `ACT_365F` da 90/365; `THIRTY_360` da 0.25.
- AC-1.2: Given el calendario `SIFMA_US` de 2026, When se pide el día hábil siguiente a un feriado listado, Then coincide con la fecha publicada por SIFMA para ese feriado. `[manual-check]` para la lista fuente; automatizado contra un fixture.
- AC-1.3: Given un año, When se piden las fechas IMM, Then son los terceros miércoles de mar/jun/sep/dic.
- AC-1.4: Given una convención no soportada (p. ej. `"ACT/ACT ISMA"`), When se construye un instrumento con ella, Then se lanza `UnsupportedConventionError` con el nombre de la convención; nunca un default.

### US-2: Datos de mercado con procedencia
As a user, I want cargar SOFR, promedios, índice, EFFR, Treasuries y settlements SR1/SR3 desde fuentes gratuitas o archivo, so that cada curva sepa de dónde salió cada input.
- AC-2.1: Given `fred` como proveedor, When se piden `SOFR`, `SOFR30DAYAVG`, `SOFR90DAYAVG`, `SOFRINDEX`, `EFFR`, `DGS2`, Then el `MarketSnapshot` contiene cada serie con `Provenance(source, series_id, retrieved_at)`.
- AC-2.2: Given un CSV de settlements SR1/SR3 (formato CME público o fixture), When se carga, Then cada contrato queda con `contract_month`, `settlement_price`, `settlement_date` y procedencia `file`.
- AC-2.3: Given una fecha hábil sin fixing SOFR en el snapshot, When se construye una curva que la necesita, Then se lanza `MissingFixingError` nombrando la fecha; nunca se interpola un fixing.
- AC-2.4: Given un día no hábil dentro de un periodo de composición, When se compone SOFR, Then se repite la última tasa publicada (regla CME/NY Fed) y la evidencia registra cuántos días se repitieron.
- AC-2.5: Given `import rates_engine`, When se importa, Then no se toca la red ni se instalan filtros de warnings (test de side effects).
- AC-2.6: Given una serie `DGS*` cargada como insumo de curva, When entra al snapshot, Then su `Provenance` lleva `instrument_kind="treasury_par_yield"` y `data_quality="proxy"`; el snapshot nunca la presenta como par OIS.

### US-3: Bootstrap de la curva de descuento SOFR
As a quant, I want construir la curva de descuento OIS-SOFR desde fixings, futuros SR1/SR3 (ajustados por convexidad) y, para el largo plazo, puntos par de proxy Treasury declarados como tales, so that pueda descontar cualquier flujo colateralizado a la tasa de colateral.
- AC-3.1: Given un conjunto de instrumentos {stub SOFR realizado, N futuros SR3 ajustados, M puntos par del largo plazo}, When se hace `bootstrap_discount_curve`, Then cada instrumento de entrada re-precia a residuo < 0.01 bp (roundtrip).
- AC-3.2: Given la curva resultante, When se evalúa `df(t)` en cualquier t, Then es positiva y no creciente en t; si los inputs implican lo contrario, se lanza `CurveArbitrageError` nombrando el tramo.
- AC-3.3: Given la misma entrada y el mismo intérprete, When se hace bootstrap dos veces, Then los DFs son bit-a-bit idénticos (idempotencia). Given la misma entrada en distintas versiones de la matriz de CI, When se comparan los DFs, Then coinciden a 1e-14 relativo. **No se exige bit-exactitud entre versiones de Python/numpy/BLAS**: no es garantizable y produciría flakes.
- AC-3.4: Given un instrumento cuyo residuo supera la tolerancia configurada, When termina el bootstrap, Then no se descarta silenciosamente: aparece en `evidence.dropped_instruments` con la razón, o el bootstrap se rehúsa según `strict=True`.
- AC-3.5: Given la evidencia del bootstrap, When se inspecciona, Then contiene: instrumentos usados con procedencia, residuos por instrumento en bp, nodos, método de interpolación (`log_linear_df`), modelo y σ de convexidad aplicados a los futuros.
- AC-3.6: Given la intención de usar Treasuries como proxy del par OIS, When se construye la curva, Then hay que pedirlo explícitamente con `long_end_source="treasury_proxy"`; **no existe default**. Omitirlo con instrumentos `DGS*` presentes lanza `ProxySourceNotDeclaredError`.
- AC-3.7: Given `long_end_source="treasury_proxy"`, When termina el bootstrap, Then (a) cada nodo cuyo valor depende de un punto Treasury queda con `data_quality="proxy"`, (b) `evidence.warnings` contiene una entrada nombrando el swap spread como sesgo presente y no cuantificado, y (c) el payload JSON expone `long_end_source` en el nivel superior. Test: la curva construida solo con futuros no lleva ninguna de las tres marcas.
- AC-3.8: Given cualquier golden test de §Definition of Done, When se ejecuta, Then su curva no contiene nodos `data_quality="proxy"`. Ningún número publicado se valida contra una curva contaminada por el proxy.

### US-4: Cuatro vistas de la curva y conversiones
As a CFA-level user, I want obtener la curva de descuento, la curva cero (spot), la curva par y la curva forward, y convertir entre ellas, so that pueda razonar con las relaciones de no-arbitraje del temario (L1 FI Valuation, L2 Term Structure).
- AC-4.1: Given una `DiscountCurve`, When se pide `zero_curve(compounding="continuous"|"annual"|"simple", day_count)`, Then $z(t) = -\ln P(0,t)/t$ (continuo) y las otras convenciones son consistentes con ella a 1e-12.
- AC-4.2: Given la curva cero, When se pide el forward $f(t_1,t_2)$, Then cumple $(1+s_{t_2})^{t_2} = (1+s_{t_1})^{t_1}(1+f)^{t_2-t_1}$ (forma del wiki, [[CFA Fixed Income — Valuation]]) a 1e-12.
- AC-4.3: Given la curva de descuento, When se pide `par_curve(tenors, frequency, day_count)`, Then para cada tenor la tasa par es la que hace $PV = 0$ de un OIS/IRS con esa frecuencia; verificado creando el swap y valuándolo con `pv()`.
- AC-4.4: Given una curva par sintética (p. ej. plana 4%), When se hace bootstrap → cero → par, Then se recupera la curva par original a 1e-8 (roundtrip par→cero→par).
- AC-4.5: Given las cuatro vistas, When se exportan, Then el payload incluye para cada una: convención de composición, day count, y nodos; nunca una tasa sin su convención.

### US-5: Futuros SOFR y ajuste de convexidad bajo descuento a tasa de fondeo
As a quant, I want valuar SR1 y SR3, calcular su tasa forward implícita y el ajuste futuros-vs-forward, so that pueda usar futuros como instrumentos de curva sin sesgo.
- AC-5.1: Given fixings SOFR de un mes calendario ya cerrado, When se calcula el settlement SR1, Then es $100 - \bar{r}$ con media aritmética ACT/360 y coincide con el settlement final publicado por CME a < 0.1 bp (fixture de ≥ 6 contratos expirados).
- AC-5.2: Given fixings entre dos terceros miércoles, When se calcula el settlement SR3, Then es $100 - R$ con $R$ compuesto ACT/360 y coincide con CME a < 0.1 bp (fixture de ≥ 6 contratos).
- AC-5.3: Given un futuro SR3 con precio $P$, σ y modelo `ho_lee`, When se pide el ajuste, Then $\text{adj} = \tfrac12\sigma^2 T_1 T_2$ y la tasa forward = $(100-P)/100 - \text{adj}$; contra la tabla Hull/Hendricks con σ = 1%: 0.62 bp a 1y, 2.25 bp a 2y (±0.01 bp).
- AC-5.4: Given modelo `hull_white(kappa, sigma)`, When κ → 0, Then el ajuste converge al de `ho_lee` (±0.01 bp) — test de consistencia entre modelos.
- AC-5.5: Given σ proveniente de `realized_sofr(window=252)`, When se calcula, Then la evidencia registra ventana, fechas y valor; si la ventana tiene < 60 observaciones se rehúsa con `InsufficientDataError`.
- AC-5.6: Given la tira de futuros, When se reporta el ajuste por contrato, Then adj(< 1y) < 1 bp y adj crece con T (monotonía), consistente con Skov-Skovmand ("material solo > 2y").
- AC-5.7: Given un futuro valuado, When se inspecciona la evidencia, Then indica que el descuento se hizo a la tasa de colateral (curva OIS-SOFR) y no a una curva de fondeo distinta — la razón de Fujii-Shimada-Takahashi / Piterbarg, con cita al artículo del wiki en el docstring.

### US-6: Valuación de OIS, IRS y FRA
As a user, I want PV, tasa par, anualidad y DV01 de OIS, IRS fijo-vs-Term SOFR 3M y FRA, so that pueda valuar y medir riesgo de los instrumentos lineales básicos.
- AC-6.1: Given un OIS creado a su `par_rate`, When se calcula `pv`, Then $|PV| < 10^{-8}$ del nocional.
- AC-6.2: Given un OIS payer y el mismo receiver, When se calcula `dv01` (bump paralelo 1 bp), Then signos opuestos y magnitud igual a 1e-10.
- AC-6.3: Given un IRS fijo-vs-Term SOFR 3M con curva de tenor ≠ curva OIS, When se valúa, Then los flujos flotantes se proyectan en la curva de tenor y se descuentan en la OIS (Mercurio/Bianchetti); test: con tenor = OIS, el resultado coincide con el OIS equivalente. Curva de tenor de origen sintético en v1 (ver US-7).
- AC-6.4: Given un FRA, When se calcula su tasa justa en multi-curva, Then difiere del forward simple de la curva de descuento cuando hay basis, y coincide cuando basis = 0 (test de las dos ramas).
- AC-6.5: Given `dv01(instrument, curve_set, bump_bp=1)`, When se calcula, Then es un shift **paralelo** con re-precio completo y la evidencia declara `bump_bp` y unidad (USD/bp). Key rate, duración y convexidad viven en **US-10**, no aquí.
- AC-6.6: Given un swap de 2 años, When se valúa contra la curva CME 2025 reconstruida desde los precios SR3 que publica el whitepaper, Then el cupón IMM par es **3.3304% ± 0.5 bp** (whitepaper CME/Rogerson 2025, [[SOFR Futures — Pricing, Convexity and Hedging Swaps]]).

### US-7: Bootstrap dual-curva (solver validado contra curvas sintéticas)
As a quant, I want resolver simultáneamente curva OIS y curva de tenor con basis swaps, so that mida la diferencia entre bootstrap secuencial y simultáneo.

> **Alcance v1 (decisión C1):** no existe fuente gratuita de par Term SOFR 3M ni de basis swaps OIS-vs-Term. En v1 los inputs de esta US son **sintéticos** (curvas y quotes construidas en fixtures a partir de un basis conocido). Se valida que el solver es correcto, no que reproduce un mercado. El proveedor de datos reales entra en v1.1 junto con la fuente de pares.

- AC-7.1: Given OIS par, IRS par sobre tenor y basis swaps **sintéticos**, When se resuelve con `mode="simultaneous"`, Then todos los instrumentos re-precian a < 0.01 bp.
- AC-7.2: Given el mismo set sintético, When se compara con `mode="sequential"`, Then el payload reporta la diferencia por tenor en bp; con basis ≡ 0 la diferencia es < 0.01 bp.
- AC-7.3: Given la curva de tenor y la OIS, When se calcula el forward basis $BA_{fd}$ (Bianchetti eq. 20), Then reproduce el basis swap sintético de entrada a < 0.1 bp.
- AC-7.4: Given un sistema mal condicionado (más nodos que instrumentos), When se resuelve, Then se rehúsa con `UnderdeterminedCurveError` nombrando nodos sin instrumento.
- AC-7.5: Given `solve_dual_curve` con inputs sintéticos, When se inspecciona la evidencia, Then declara `inputs_origin="synthetic"`; no hay ruta de datos reales en v1 y pedirla lanza `NoTenorQuoteSourceError`.

### US-8: Cobertura de un swap con tira de futuros
As a risk manager, I want el número de contratos SR3 por periodo IMM para cubrir un swap y una tabla de P&L bajo shocks, so that vea el residuo de convexidad en dólares y en bp.
- AC-8.1: Given un OIS 2y de USD 100M contra la tira SR3 (curva CME 2025 reconstruida desde los precios del whitepaper), When se pide `strip_hedge`, Then el total es **779 ± 2 contratos** y la distribución por periodo suma ese total.
- AC-8.2: Given la cobertura anterior, When se aplica shock −100 bp, Then el P&L neto (swap + tira) es **+22,292 USD ± 3%** y el DV01 del swap pasa de **19,480 a 19,921 ± 3%**.
- AC-8.3: Given `shock_table` con shocks ±10/25/50/100 bp, When se genera, Then por fila reporta P&L swap, P&L tira, neto, DV01 post-shock y **neto/DV01 en bp** (la "convexidad implícita en P&L").
- AC-8.4: Given una tira con un contrato faltante para un periodo del swap, When se pide la cobertura, Then se rehúsa con `IncompleteStripError` nombrando el periodo; nunca se extrapola un contrato.
- AC-8.5: Given DV01 por contrato, When se deriva, Then sale del nocional y el plazo, no de una tabla: SR3 = 1,000,000 × 0.25 × 0.0001 = **USD 25.00/bp**; SR1 = 5,000,000 × (30/360) × 0.0001 = **USD 41.67/bp**. El test verifica la derivación, no el literal. **[asumo]** el nocional de SR1 = USD 5,000,000 (spec CME no leída); la evidencia marca `notional_source="assumed"` para SR1 hasta confirmarlo, y `"derived"` para SR3.

### US-9: Evidencia, refusals y superficie JSON
As a coding agent or pipeline, I want cada resultado con `value` + `evidence`, errores nombrados y `--json` con `schema_version`, so that pueda actuar sin scrapear texto.
- AC-9.1: Given cualquier resultado público (`BootstrapResult`, `PriceResult`, `HedgeResult`), When se serializa, Then contiene `schema_version`, y todo campo ausente es `null`, nunca key faltante.
- AC-9.2: Given `rateng bootstrap --config c.yaml --json`, When se ejecuta, Then stdout es un solo documento JSON parseable y toda narración va a stderr.
- AC-9.3: Given un comando que falla antes de producir resultado, When se ejecuta con `--json`, Then stdout contiene `{error, exit_code}` y el traceback va a stderr; exit code 1 datos inutilizables, 2 curva/mandato imposible.
- AC-9.4: Given `src/rates_engine`, When se escanea, Then no existe ningún `except: pass` ni `except Exception:` sin re-raise o registro en evidencia (test `no_silent_swallow`).
- AC-9.5: Given `docs/ERRORS.md`, When se compara con las excepciones definidas, Then cada excepción pública aparece con: qué significa, si es recuperable, y qué hacer.
- AC-9.6: Given todos los nombres públicos, When se mide cobertura de docstrings, Then es 100% y cada parámetro numérico declara unidad (bp, USD/bp, años ACT/360).
- AC-9.7: Given un `HedgeResult` producido a partir de un `PriceResult` que a su vez usó un `BootstrapResult`, When se inspecciona `evidence`, Then la evidencia del hedge contiene la del precio y la del bootstrap como `evidence.sources: list[Evidence]` (encadenamiento, no aplanado ni pérdida), y `to_dict()` serializa la cadena completa de forma recursiva. **Decisión de arquitectura: `Evidence` es un tipo único y componible desde el primer commit**; retrofitearlo en fase de hedging es caro.
- AC-9.8: Given cualquier `Evidence` en la cadena, When contiene un nodo o serie con `data_quality != "observed"`, Then esa marca se propaga hacia arriba: un `HedgeResult` construido sobre curva con proxy declara el proxy en su nivel superior. La degradación no se pierde al componer.

### US-10: Medidas de riesgo — key rate, duración y convexidad
As a risk manager and CFA-level user, I want KR DV01 con base y forma de bump declaradas, más las convenciones de duración que la curva permite calcular honestamente, so that pueda atribuir riesgo por tramo y usar el vocabulario del temario sin inventar medidas indefinidas.

**Key rate DV01 (base: nodo de curva cero, bumps tent)**
- AC-10.1: Given `key_rate_dv01(instrument, curve_set, key_tenors=[...])`, When se construyen los shocks, Then son **triangulares (tent) sobre la curva cero**, con vértice en cada key tenor y soporte hasta los key tenors vecinos, de modo que **la suma de los shocks es exactamente un shift paralelo de 1 bp** (partición de la unidad). El test verifica los shocks mismos, no solo el resultado.
- AC-10.2: Given esos bumps, When se suman los KR DV01, Then iguala el DV01 paralelo de AC-6.5 a 1e-6. (Este es el test del viejo AC-6.5, ahora con la condición que lo hace cierto.)
- AC-10.3: Given el resultado, When se inspecciona la evidencia, Then declara `bump_basis="zero_curve_node"`, `bump_shape="tent"`, los key tenors, los nodos de la curva tocados por cada bump y el método de interpolación, **junto con la advertencia de que el perfil key rate depende de la colocación de nodos y de la interpolación**: dos curvas que precian idéntico pueden dar perfiles distintos. La advertencia es un campo, no un comentario en el docstring.
- AC-10.4: Given un key tenor fuera del rango de la curva, When se pide, Then se rehúsa con `KeyTenorOutOfRangeError` nombrando el tenor y el rango disponible; nunca se extrapola.
- AC-10.5: Given `key_rate_duration` (KR DV01 normalizado por precio), When |PV| < 1e-8 × nocional (swap a par), Then se rehúsa con `UndefinedDurationError` explicando que con PV = 0 la medida normalizada no existe y que la medida correcta es `key_rate_dv01`. Given un instrumento con PV ≠ 0, Then devuelve KR DV01 / (PV × 1bp) con unidad declarada.

**Reconciliación con la vista por instrumento del hedge**
- AC-10.6: Given el `HedgeResult` de US-8, When se inspecciona, Then la exposición por periodo IMM se expone explícitamente como `bucketed_delta_by_instrument` con `bump_basis="instrument_quote"` en su evidencia — es un delta bucketeado por instrumento, no un key rate por nodo, y el payload lo nombra así.
- AC-10.7: Given las dos vistas sobre el mismo swap y la misma curva, When se comparan, Then ambas suman el mismo DV01 paralelo a 1e-6 y el payload reporta la diferencia por tramo en USD/bp **sin declarar que una es la correcta**: responden preguntas distintas (riesgo de curva vs. riesgo de cobertura).

**Convenciones de duración y convexidad**
- AC-10.8: Given `pvbp(instrument, curve_set)` y el `dv01` paralelo de AC-6.5, When se comparan, Then son el mismo número (test de equivalencia, no dos implementaciones). `money_duration` = dP/dy = DV01 × 10,000, y cada uno declara su unidad: USD por unidad de rendimiento vs USD/bp. Nunca se devuelve un número de duración sin unidad.
- AC-10.9: Given `money_convexity(instrument, curve_set, bump_bp)`, When se calcula, Then es (PV₊ + PV₋ − 2·PV₀)/Δy² por re-precio completo con shift paralelo, en **USD por unidad de rendimiento al cuadrado**, y está definido también cuando PV₀ = 0 (por eso existe además de la versión normalizada).
- AC-10.10: Given la posición cubierta de US-8 (swap + tira, DV01 ≈ 0), When se predice el P&L con ½·`money_convexity`·Δy², Then coincide con el re-precio completo del `shock_table` dentro de **1% a ±10 bp y 10% a ±100 bp**. Es la reconciliación entre la convexidad medida y la observada; una discrepancia mayor significa que uno de los dos está mal.
- AC-10.11: Given `effective_duration(instrument, curve_set, bump_bp)`, When se calcula, Then es (PV₋ − PV₊)/(2·PV₀·Δy) con shift paralelo de la **curva** y re-precio completo, y la evidencia declara `bump_bp` y que es *effective* — derivada de la curva, no de un rendimiento único. `effective_convexity` = (PV₊ + PV₋ − 2·PV₀)/(PV₀·Δy²) bajo la misma definición.
- AC-10.12: Given `effective_duration` o `effective_convexity` sobre un instrumento con |PV| < 1e-8 × nocional, When se calculan, Then se rehúsan con `UndefinedDurationError`; para un swap a par las vistas válidas son `dv01`, `money_convexity` y el `shock_table` de AC-8.3. Test: el mismo OIS a par rehúsa la medida normalizada y devuelve la monetaria.
- AC-10.13: Given `macaulay_duration` o `modified_duration`, When se invocan, Then existen como **stubs explícitos** que lanzan `NotImplementedError` explicando que requieren un rendimiento único y por tanto un instrumento de precio ≠ 0 (`FixedRateBond`, v1.1). Existen para que un agente que las busque reciba la razón y no un `AttributeError`. **Nunca se aproxima una con `effective_duration` bajo otro nombre.**

## Constraints
- **Python ≥ 3.11** (decisión D1: 3.9 está EOL desde oct-2025 y ata a numpy 1.x). Dependencias core solo `numpy>=2.0`, `pandas>=2.2`, `scipy`. Extras: `data` (pyarrow), `dev` (pytest, hypothesis, ruff, mypy con allowlist que solo encoge), `docs` (pdoc).
- Layout `src/rates_engine/`, distribución `finport-ratesengine`, CLI `rateng`. Convenciones espejo de `alanvaa06/optimization_engine` v0.7.0: `AGENTS.md`, `llms.txt`, `CHANGELOG.md` (Keep a Changelog), `docs/ERRORS.md`, `docs/RESEARCH.md` (mapa a artículos del wiki), `py.typed`.
- Determinismo: sin aleatoriedad en v1; mismo input + mismo intérprete → mismo output bit-a-bit; entre versiones de la matriz, 1e-14 relativo (AC-3.3).
- Windows-compatible (Alan en win32): sin comandos POSIX-only en scripts; salida de consola ASCII.
- Sin red en import ni en tests; los tests usan fixtures versionados (settlements CME y fixings FRED congelados con fecha).
- Tolerancias de golden tests fijadas arriba; un cambio de tolerancia requiere entrada en CHANGELOG. **Si un golden no entra en tolerancia, se marca `xfail` con la razón documentada; nunca se afloja la tolerancia para que pase** (cláusula de la decisión A1).
- Construcción en `C:\Proyectos\rates_engine`, nunca en el vault.

## Orden de construcción propuesto (primer corte vertical)
No vinculante; entrada para `plan-design`. El objetivo es poner el golden test CME como forzante temprano.

1. `conventions` (US-1) → `market` solo con proveedor `file` (US-2 parcial) → settlement SR1/SR3 contra fixture CME (AC-5.1, AC-5.2).
2. Ajuste Ho-Lee (AC-5.3) → `bootstrap_discount_curve` solo desde futuros (US-3 sin proxy) → `pv`/`dv01` de OIS (AC-6.1, AC-6.2).
3. `strip_hedge` + `shock_table` → goldens 779 / +22,292 (US-8) → CLI `--json` (US-9 parcial).
4. Después: proveedor `fred`, proxy Treasury (AC-3.6–3.8), vistas par/cero/forward (US-4), medidas de riesgo (US-10), dual-curva sintética (US-7).

## Definition of Done
- [ ] Todos los AC verdes (`pytest -q`), incluidos property tests con `hypothesis`.
- [ ] `[manual-check]` AC-1.2 verificado una vez contra la lista SIFMA y anotado.
- [ ] `ruff check` y `mypy` verdes (allowlist inicial permitida, ceiling en test).
- [ ] `AGENTS.md`, `llms.txt`, `docs/ERRORS.md`, `docs/RESEARCH.md`, `CHANGELOG.md` presentes; README con install y quickstart cuyo output impreso está verificado por test.
- [ ] CI: ruff, matriz 3.11–3.13, core-install job, CLI smoke con `--json`.
- [ ] Goldens CME (AC-6.6, AC-8.1, AC-8.2) verdes sobre curva reconstruida desde el whitepaper, o marcados `xfail` con la hipótesis de reconstrucción que falla documentada en el fixture.
- [ ] Tag `v0.1.0` y publicación a TestPyPI vía Trusted Publishing; PyPI solo con aprobación de Alan.

## Decisiones cerradas (2026-09-16, Alan)
| # | Pregunta | Elegido | Consecuencia en este PRD |
|---|---|---|---|
| A | Curva de entrada de los golden tests CME | **Reconstruir desde los precios SR3 del whitepaper**, con las hipótesis de reconstrucción en el fixture | AC-6.6, AC-8.1, AC-8.2; cláusula `xfail` en Constraints y DoD |
| B | Fuente de par OIS para el largo plazo | **Proxy Treasury `DGS*` con warning** | AC-2.6, AC-3.6, AC-3.7, AC-3.8; Non-Goal de fuentes licenciadas |
| C | Alcance de US-7 (dual-curva) | **Queda en v1, solo con inputs sintéticos** | US-7 reencabezada; AC-7.5; AC-6.3 anotado |
| D | Baseline de Python | **≥ 3.11** | Constraints; matriz de CI 3.11–3.13 |
| E | Alcance del key rate | **US propia con base decidida**: bumps tent sobre nodos de curva cero, evidencia que declara base/forma/dependencia de interpolación, refusal de la medida normalizada a PV = 0, y reconciliación con el delta por instrumento del hedge | US-10 (AC-10.1–10.7); AC-6.5 reducido al DV01 paralelo |
| F | Convenciones de duración | **Solo lo que el bump sostiene**: `pvbp`, `money_duration`, `money_convexity`, `effective_duration`, `effective_convexity`, con refusals. Macaulay/modified y `FixedRateBond` → v1.1 | US-10 (AC-10.8–10.13); Non-Goals |

Riesgo aceptado en B: el proxy Treasury incorpora swap spread (negativo y variable, [[Swap Spreads — Credit, Duration Demand and Limits to Arbitrage]]) en todo nodo largo. Mitigación contratada: opt-in explícito, marca `data_quality="proxy"` propagada hacia arriba (AC-9.8), y prohibición de usarlo en goldens (AC-3.8).
