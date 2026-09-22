# Arquitectura de `rates_engine` (reorganización v0.4)

*Escrito 2026-09-22 sobre `main` @ `cf3c2f1` (v0.3.0: 59 archivos `.py`, 14.9k líneas, 1612 tests). Rama `refactor/architecture`. Estado: **fases 1-5 implementadas y verificadas; las dos decisiones de §8 se tomaron (1a, 2a) y están implementadas.***

Este documento reemplaza la sección de módulos de `docs/design/2026-09-16-rates-engine-design.md`, que describía la fase 1 antes de que existiera código. Los PRDs siguen vigentes: la reorganización no cambia ningún número ni ninguna refusal documentada.

---

## 1. Diagnóstico y estado

La calidad por función era alta: evidencia encadenada, refusals nombradas, 1612 tests. Los problemas estaban en la **estructura**, y cada uno se comprobó en el código, no se infirió.

| # | Problema | Evidencia en v0.3.0 | Estado |
|---|---|---|---|
| D1 | No hay capas, solo un orden topológico | `test_layering.py` ordenaba 21 módulos en una lista total: `fx` quedaba *debajo* de `instruments` sin que eso significara nada. 14 módulos sueltos en la raíz junto a 7 paquetes | **Resuelto** (fase 1): 10 capas con significado; `test_layering` prohíbe aristas laterales y hacia arriba |
| D2 | La moneda es una etiqueta; índice y calendario están cableados a USD | `calendar: SIFMAUSCalendar` (tipo concreto) en 5 módulos. `pricing` y `optionpricing` escribían `"collateral_rate_ois_sofr"` en la evidencia de **cualquier** curva | **Resuelto** (fase 4 y §8.2): la evidencia se deriva del índice, y cada instrumento lleva su `RateIndex`, que fija moneda y calendario |
| D3 | Dos vocabularios de instrumentos | `OISSwap` (para valuar) frente a `ParSwapNode` (para calibrar), con su propia `annuity`/`par_rate`. El swap TIIE solo existe como nodo | **Resuelto** (§8.1): `pricing.calibration.SwapQuoteNode` calibra con el pricer real; `ParSwapNode` queda para cotizaciones que llegan como fechas |
| D4 | Los instrumentos valúan y recalculan | `OISSwap.float_cashflows(curve_set)` proyecta desde la curva. `Schedule.generate` corría en **cada** `cashflows()` | **Resuelto** (fase 2 y §8.2): el schedule se calcula una vez y la proyección vive en `pricing.projection` |
| D5 | Valuación y riesgo fragmentados | `dv01` en `pricing`, el resto en `risk`; seis medidas escribían `pv(instrument, curve_set.shifted(s)).value` por su cuenta | **Resuelto** (fase 5 y revisión): `risk.bumps.shifted` es la única llamada que mueve una curva fuera de `curves`, y `test_layering` lo exige; `dv01` vive en `risk` |
| D6 | Fórmulas mezcladas con objetos de mercado | `volatility/` tenía fórmulas, objetos y un enum de producto; Garman-Kohlhagen en `fx/`; `convexity.py` estimaba σ desde un snapshot | **Resuelto** (fase 1): `models/` = solo fórmulas; σ realizada en `market.estimators` |
| D7 | No hay capa de aplicación | `cli.py` (595 líneas) parseaba, construía y ejecutaba; `mcp_server` importaba el `_COMMANDS` **privado** de `cli` | **Resuelto** (fase 3): `app.config`, `app.builders`, `app.commands.COMMANDS`; `cli` y `mcp_server` son adaptadores hermanos |
| D8 | "Hedging" disperso | `hedging.py`, `hedging_structures.py`, `hedge_program.py` en la raíz | **Resuelto** (fase 1): paquete `hedging/` |
| D9 | Evaluación de curva sin caché | `df()` recalculaba `node_times` y `log(dfs)` en cada llamada; barrido lineal; monotone-convex reconstruido por llamada | **Resuelto** (fase 2): caché, `bisect`, prefijos acumulados; salida bit a bit idéntica |

**Qué no era un problema, aunque lo pareciera.** Los 33 tipos de excepción (32 más la base) en un solo `errors.py` están bien: es el "hogar único" que `docs/ERRORS.md` documenta. Las 157 re-exportaciones de `rates_engine/__init__.py` también: son la API estable y se conservan todas.

---

## 2. Principios

1. **Las dependencias apuntan hacia abajo por capas con significado.** Un paquete solo importa de capas inferiores. Dos paquetes de la misma capa no se conocen entre sí. Lo verifica `tests/test_layering.py`.
2. **Cada eje de crecimiento tiene un único punto de extensión** (§4).
3. **La evidencia se deriva de los objetos, no de literales.** Qué índice y qué colateral descontaron un flujo sale de la moneda de la curva.
4. **Un nombre tiene dos hogares:** la raíz `rates_engine` y el módulo que lo define. Los `__init__` de subpaquete documentan su capa y no re-exportan nada.
5. **`produced_by` es un identificador, no una ruta.** `"pricing.dv01"` sigue siendo el `produced_by` de `dv01` aunque la función viva en `risk`: el payload no cambia cuando el código se mueve.
6. **La eficiencia se mide, no se supone.** `benchmarks/bench_core.py` corre antes y después; una batería de 80 kB de salidas se compara bit a bit contra v0.3.0.

---

## 3. Capas y paquetes

```
 9  cli · mcp_server                      adaptadores: argparse / MCP → app
 8  app                                   casos de uso: config → dominio → payload
 7  reporting                             serialización JSON de resultados y errores
 6  hedging                               tira de futuros, estructuras FX, política
 5  risk                                  un bump → DV01, key rate, duraciones, griegas
 4  pricing                               PV, par, anualidad, opciones, forward FX, colateral
 3  curves · volatility · instruments     estructuras temporales · superficies · términos
 2  market · models                       datos observados · matemática cerrada
 1  conventions                           day count, calendarios, IMM, índices, vocabulario
 0  core                                  errors, evidence, results, money, diagnostics
```

| Paquete | Módulos |
|---|---|
| `core` | `errors`, `evidence`, `results`, `money`, `diagnostics` |
| `conventions` | `daycount`, `calendar`, `schedule`, `indices`, `side`, `option_kind`, `currency_pair` |
| `market` | `snapshot`, `classify`, `estimators`, `providers/{file,fred,banxico}` |
| `models` | `gaussian`, `black`, `bachelier`, `garman_kohlhagen`, `fx_delta`, `sabr`, `convexity` |
| `curves` | `discount`, `interpolation`, `bootstrap`, `dual`, `mxn`, `parametric`, `views`, `comparison` |
| `volatility` | `units`, `cube`, `fx_smile` |
| `instruments` | `cashflow`, `swaps`, `futures`, `fra`, `capfloor`, `swaption` |
| `pricing` | `linear`, `options`, `fx_forward`, `collateral` |
| `risk` | `bumps`, `sensitivities`, `greeks` |
| `hedging` | `futures_strip`, `fx_structures`, `program` |
| `reporting` | `payloads` |
| `app` | `config`, `builders`, `commands` |

**Por qué 10 capas y no 4.** Cada frontera responde una pregunta distinta: ¿cómo se cuenta?, ¿qué se observó?, ¿qué forma tiene la curva?, ¿cuánto vale?, ¿cuánto se mueve?, ¿cómo se cubre? Juntar `pricing` con `risk` volvería a permitir que el riesgo reimplemente la valuación, que es justo D5.

**Por qué el vocabulario (`Side`, `OptionKind`, `CurrencyPair`) va a `conventions`.** Lo usan las fórmulas (capa 2), los productos (3) y las coberturas (6). En `volatility` o en `instruments` obligaría a las fórmulas a importar hacia arriba.

**Por qué ya no hay paquete `fx`.** Cada pieza fue a la capa de su pregunta: el par a `conventions`, Garman-Kohlhagen y las convenciones de delta a `models`, el smile a `volatility`, el forward a `pricing`, las estructuras a `hedging`. La separación que `fx/__init__` defendía (que una convención de tasas no se aplique a FX) la siguen garantizando módulos distintos (`models.black` frente a `models.fx_delta`), no un paquete.

**Sin excepciones.** La última, `instruments -> curves`, desapareció cuando la proyección de flujos pasó a `pricing.projection` (§8.2). `test_layering` exige que toda excepción futura siga siendo necesaria, así que no puede quedarse olvidada.

---

## 4. Puntos de extensión

| Para añadir… | Se toca | No se toca |
|---|---|---|
| Una moneda (EUR) | `Currency` en `core.money`, sus `RateIndex` y su entrada en `COLLATERAL_INDEX` (`conventions/indices.py`), un calendario si es nuevo. `tests/test_indices.py` falla hasta que la moneda dice a qué tasa descuenta. Un `OISSwap(index=ESTR)` ya rueda en su calendario y se niega a valuarse en otra moneda | `curves`, `instruments`, `pricing`, `risk`, `app` |
| Una convención no verificada | Una entrada en el `unresolved` del índice y su porqué | Nada: `pricing.collateral.curve_warnings` la lleva a cada precio, cada sensibilidad y cada griega |
| Un producto lineal | Un dataclass en `instruments` con `currency` y `rate_index`, y su proyección con `pricing.projection.project.register` | `pricing.linear`, `risk` (bumpea curvas, no productos), `hedging` |
| Una fórmula de opción | Un módulo de funciones en `models` | `volatility`, `instruments` |
| Una medida de riesgo | Una función sobre `risk.bumps.repriced` | `pricing`, `curves` |
| Una interfaz (HTTP, notebook) | Un adaptador que llama a `app.commands.COMMANDS` | `cli`, `mcp_server` |

---

## 5. Eficiencia

`benchmarks/bench_core.py`, Python 3.14, Windows, mejor de 5 corridas, ms por llamada:

| Caso | v0.3.0 | v0.4 | Mejora |
|---|---:|---:|---:|
| bootstrap 40 trimestres, log-lineal | 7.70 | 2.56 | ×3.0 |
| bootstrap 40 trimestres, monotone convex | 21.82 | 10.01 | ×2.2 |
| PV swap OIS 10 años, mismo instrumento (reprecio de un libro) | 0.640 | 0.064 | ×10.0 |
| PV swap OIS 10 años, instrumento nuevo en cada llamada | 0.634 | 0.123 | ×5.2 |
| PV swap OIS 10 años, monotone convex | 1.757 | 0.080 | ×21.9 |
| DV01 swap 10 años | 1.339 | 0.151 | ×8.9 |
| key rate DV01, 10 tenores | 14.33 | 2.58 | ×5.6 |
| strip hedge 2 años, 8 trimestres | 16.06 | 8.97 | ×1.8 |
| shock table, 8 choques | 2.60 | 0.78 | ×3.3 |

Las dos columnas se midieron en la misma corrida y con el mismo script (`benchmarks/bench_core.py`, que solo usa la API raíz), aplicado a cada árbol. Las filas de "mismo instrumento" se benefician además del schedule en caché; la fila de "instrumento nuevo" no, y es la cifra honesta para una operación que se valúa una sola vez.

De dónde sale la mejora, medida con perfilador antes de tocar nada:

- **`DiscountCurve`** guarda en caché `node_times`, `log(dfs)` y el interpolante monotone-convex (`functools.cached_property` sobre el dataclass congelado; la caché no entra en `__eq__` ni en `__hash__`, y un test lo asegura). La curva que el solver crea en cada iteración (`with_node`) y la curva bumpeada (`shifted`) **heredan** esos arreglos en lugar de recalcularlos.
- **Búsqueda de tramo con `bisect`**, eligiendo en cada frontera el mismo tramo que elegía el barrido lineal, así que la aritmética no cambia.
- **`MonotoneConvex`** acumula las integrales de tramos completos en el mismo orden de suma que el bucle por llamada.
- **`Schedule`** se calcula una vez por instrumento.
- **El riesgo reprecia con `discounted_value`**, sin construir la evidencia de un `pv` que después se descarta.

**Ningún número cambió.** Una batería de 80 kB (DFs, forwards instantáneos, ceros, PV, par, anualidad, DV01, key rates de OIS e IRS payer y receiver, strip hedge y shock table, con las dos interpolaciones) sale bit a bit idéntica a v0.3.0 después de cada fase.

---

## 6. Cómo se verificó

| Chequeo | Resultado |
|---|---|
| Suite completa | 1612 → **1724** tests en verde (los nuevos: capas, caché, índices, degradaciones en riesgo, calibración con swaps, docstrings de la capa `app`) |
| Criterios de aceptación (`scripts/audit_acceptance.py`) | 109 cubiertos, 0 sin cubrir, 5 parciales (los mismos 5 que dependen de números publicados por CME) |
| Equivalencia numérica contra v0.3.0 | Bit a bit idéntica tras cada fase |
| Payloads de la CLI contra v0.3.0 | `bootstrap` idéntico byte a byte. Tras §8.2, `price` y `list-instruments` vuelven a ser idénticos byte a byte (`compounded_sofr`). Cambios intencionales, todos en texto de evidencia: la nota del bucketed delta apunta a `rates_engine.key_rate_dv01`; `describe` lista la convención MXN `mxn_collateral_rate` y los `rate_indices`; la evidencia de un `FRA` gana la clave `index` |
| `ruff`, `mypy` | Limpios (72 archivos; `mypy --python-version 3.12`, porque los stubs de numpy instalados localmente no parsean con el objetivo 3.11 del config, igual que en v0.3.0) |
| Revisión independiente | Un agente revisor comparó 113 328 evaluaciones de curva y 544 payloads de riesgo y opciones contra v0.3.0: todos idénticos. Encontró 6 defectos (test de capas ciego a imports relativos, degradaciones que no llegaban al riesgo, dos bumps por fuera de `risk.bumps`, caché obsoleta con listas mutables, afirmaciones infladas en docs, un literal SOFR). Los seis se corrigieron y cada corrección tiene un test que falla sin ella |

---

## 7. Fases ejecutadas

| Fase | Commit | Qué |
|---|---|---|
| 1 | `730184c` | Paquetes por capa, `fx` disuelto, `__init__` sin re-exportaciones, `test_layering` por capas |
| 2 | `1208398` | Caché y `bisect` en curvas, prefijos en monotone convex, schedule en caché |
| 3 | `311bcfb` | Capa `app`; `mcp_server` deja de importar `cli` |
| 4 | `fc79148` | `conventions.indices`, `pricing.collateral`; la evidencia MXN deja de decir SOFR |
| 5 | `b7ca83c` | `risk.bumps` como primitiva única; `dv01` a `risk`; griegas a `risk.greeks` |
| Revisión | `f577a35` | Correcciones de la revisión independiente (§6) |
| §8.2 | `d02d013` | Los instrumentos llevan su `RateIndex`; la proyección pasa a `pricing.projection` |
| §8.1 | `342f6bb` | `SwapQuoteNode`: nodos de calibración desde swaps reales |
| Revisión 2 | (siguiente commit) | Ocho hallazgos de una segunda revisión independiente (§8.3) |

---

## 8. Decisiones (tomadas por Alan el 2026-09-22)

Las dos revertían o ampliaban decisiones de diseño anteriores, así que se presentaron como opciones antes de implementarse.

### 8.1 Nodos de calibración desde instrumentos: opción (a)

`curves` sigue sin conocer productos. El nodo que envuelve un swap, `SwapQuoteNode`, vive en `pricing.calibration` y cumple el protocolo `CalibrationInstrument` que define `curves`: la dependencia se invierte, en lugar de que la curva importe el instrumento. Su residuo es `par_rate_value`, la misma función que después valúa el swap.

Lo que se descubrió al implementarlo: la duplicación no era inocua, y el diagnóstico tuvo dos capas. Primero apareció un descuadre de 1.7 pb en el swap a 2 años de la tira del 2026-01-15, causado por fines de período sin ajustar mientras los pagos se movían por feriado. Eso se corrigió en §8.4. Lo que queda es de fondo: `ParSwapNode` valúa la pata flotante como `P(start) - P(último pago)`, exacto solo si cada período se paga el día que termina. Un OIS SOFR paga dos días hábiles después, y una curva ajustada a `ParSwapNode` construidos con esas fechas de pago valúa mal el swap a 1 año en 5.5 pb. Sin lag, los dos nodos dan la misma curva al 1e-12 (`tests/test_calibration.py`). `SwapQuoteNode` solo acepta OIS: una cotización de tasa a plazo calibra la curva de proyección, que es trabajo del solver dual.

### 8.2 Los instrumentos llevan su índice: opción (a)

Revierte la decisión del deep review de v0.3 ("un instrumento se denomina por la curva en la que se valúa"). Cada instrumento lleva un `RateIndex` que fija su moneda y, salvo que se indique otro, su calendario; valuarlo sobre una curva de otra moneda lanza `CurrencyMismatchError`. La proyección de flujos pasó a `pricing.projection` (`project`, `float_leg`, registrables con `singledispatch`), y con ella desapareció la última excepción de capas. Las convenciones no verificadas del índice del instrumento llegan a cada precio, sensibilidad y griega.

Los números en dólares siguen bit a bit idénticos a v0.3.0. El payload de `price` vuelve a ser byte a byte el de v0.3.0 (`float_index: compounded_sofr`), porque ahora el swap sabe su índice en lugar de tenerlo cableado.

**Lo que sigue abierto:** el swap TIIE 28 acumula periodos de 28 días que `Schedule` (en meses) no genera, así que existe como nodo (`tiie_par_swap_node`) pero no como instrumento. `IRSwap(index=TIIE_28)` se rechaza con un mensaje que lo dice.

### 8.3 Segunda revisión independiente

Un agente revisor comparó 112 resultados contra v0.3.0 (OIS, IRS 3M/6M, FRA, caps/floors y swaptions con griegas, key rates, strip hedge) y todos salieron idénticos. Encontró ocho defectos, todos corregidos, cada uno con un test en `tests/test_review_index_branch.py` que falla sin la corrección:

1. `SwapQuoteNode.node_date` fijaba el último pago aunque *modified following* lo moviera antes del fin de período sin ajustar; el ajuste a 3 años de una tira de fin de mes fallaba. Ahora es el máximo de ambos.
2. Nodos sin etiqueta compartían clave en el dict de residuos y el chequeo estricto leía solo el último: un ajuste fallido pasaba sin verse. Afectaba también a `FuturesNode`. El chequeo ahora es por instrumento y las claves repetidas se desambiguan con la fecha del nodo.
3. Caps y floors se valuaban sobre una curva de otra moneda sin rechazarlo.
4. La CLI construía una curva en pesos a partir de futuros SOFR. Ahora `price` rechaza un swap no USD.
5. Una curva calibrada a swaps TIIE no llevaba sus supuestos. Ahora el bootstrap recoge las `degradations` de los nodos.
6. Rupturas no documentadas: frecuencias sin Term SOFR publicado y la clave `index` de `FRA`; además `FRA` aceptaba un índice overnight.
7. Docs y payloads desactualizados (`list-instruments` vuelve a `compounded_sofr`) y una afirmación inflada: un producto propio necesita `float_leg.register` además de `project.register` para `par_rate`.
8. Tres escrituras del mismo IRS eran distintas para `==` y `hash`. Frecuencia e índice ahora se normalizan en ambos campos.

### 8.4 Fines de período ajustados (decisión de Alan, opción a)

La primera versión de §8.1 atribuía el descuadre de 1.7 pb a `ParSwapNode`. La revisión lo acotó: el origen era la convención de schedule de esta librería, que dejaba los fines de período sin ajustar mientras movía los pagos. La convención de mercado para OIS SOFR ajusta ambos. Así lo describen el reglamento del contrato Eris SOFR de CME (periodos que terminan en el aniversario "subject to adjustment in accordance with the Modified Following Business Day Convention", con pago dos días hábiles después, Following) y la nota de ISDA sobre las definiciones 2006 (los *Period End Dates* se ajustan junto con las fechas de pago, Modified Following por defecto). Ambas se leyeron vía resúmenes de búsqueda; los PDFs de CME no cargaron desde este entorno.

`Schedule.generate` ahora ajusta los fines de período con la misma convención que los pagos, y `adjust_accrual_ends=False` reproduce el comportamiento anterior. Es el único cambio que mueve números en dólares, y solo donde una fecha de aniversario no es día hábil. En el quickstart del README, la tasa par pasa de 4.0539% a 4.0537%. Con lag 0, `ParSwapNode` y `SwapQuoteNode` coinciden ahora también donde las fechas se mueven.
