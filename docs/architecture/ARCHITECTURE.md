# Arquitectura de `rates_engine` (reorganización v0.4)

*Escrito 2026-09-22 sobre `main` @ `cf3c2f1` (v0.3.0: 60 módulos, 14.9k líneas, 1612 tests). Rama `refactor/architecture`. Estado: **fases 1-5 implementadas y verificadas**; quedan dos decisiones abiertas para Alan (§8).*

Este documento reemplaza la sección de módulos de `docs/design/2026-09-16-rates-engine-design.md`, que describía la fase 1 antes de que existiera código. Los PRDs siguen vigentes: la reorganización no cambia ningún número ni ninguna refusal documentada.

---

## 1. Diagnóstico y estado

La calidad por función era alta: evidencia encadenada, refusals nombradas, 1612 tests. Los problemas estaban en la **estructura**, y cada uno se comprobó en el código, no se infirió.

| # | Problema | Evidencia en v0.3.0 | Estado |
|---|---|---|---|
| D1 | No hay capas, solo un orden topológico | `test_layering.py` ordenaba 21 módulos en una lista total: `fx` quedaba *debajo* de `instruments` sin que eso significara nada. 14 módulos sueltos en la raíz junto a 7 paquetes | **Resuelto** (fase 1): 10 capas con significado; `test_layering` prohíbe aristas laterales y hacia arriba |
| D2 | La moneda es una etiqueta; índice y calendario están cableados a USD | `calendar: SIFMAUSCalendar` (tipo concreto) en 5 módulos. `pricing` y `optionpricing` escribían `"collateral_rate_ois_sofr"` en la evidencia de **cualquier** curva | **Resuelto en la evidencia** (fase 4): `conventions.indices` + `pricing.collateral`; un precio MXN dice TIIE de Fondeo y queda `assumed`. **Abierto en los instrumentos** (§8.2) |
| D3 | Dos vocabularios de instrumentos | `OISSwap` (para valuar) frente a `ParSwapNode` (para calibrar), con su propia `annuity`/`par_rate`. El swap TIIE solo existe como nodo | **Abierto** (§8.1) |
| D4 | Los instrumentos valúan y recalculan | `OISSwap.float_cashflows(curve_set)` proyecta desde la curva. `Schedule.generate` corría en **cada** `cashflows()` | **Recálculo resuelto** (fase 2: el schedule se calcula una vez). **Proyección abierta** (§8.2) |
| D5 | Valuación y riesgo fragmentados | `dv01` en `pricing`, el resto en `risk`; seis medidas escribían `pv(instrument, curve_set.shifted(s)).value` por su cuenta | **Resuelto** (fase 5): `risk.bumps` es la única primitiva de bump; `dv01` vive en `risk` |
| D6 | Fórmulas mezcladas con objetos de mercado | `volatility/` tenía fórmulas, objetos y un enum de producto; Garman-Kohlhagen en `fx/`; `convexity.py` estimaba σ desde un snapshot | **Resuelto** (fase 1): `models/` = solo fórmulas; σ realizada en `market.estimators` |
| D7 | No hay capa de aplicación | `cli.py` (595 líneas) parseaba, construía y ejecutaba; `mcp_server` importaba el `_COMMANDS` **privado** de `cli` | **Resuelto** (fase 3): `app.config`, `app.builders`, `app.commands.COMMANDS`; `cli` y `mcp_server` son adaptadores hermanos |
| D8 | "Hedging" disperso | `hedging.py`, `hedging_structures.py`, `hedge_program.py` en la raíz | **Resuelto** (fase 1): paquete `hedging/` |
| D9 | Evaluación de curva sin caché | `df()` recalculaba `node_times` y `log(dfs)` en cada llamada; barrido lineal; monotone-convex reconstruido por llamada | **Resuelto** (fase 2): caché, `bisect`, prefijos acumulados; salida bit a bit idéntica |

**Qué no era un problema, aunque lo pareciera.** Los 31 tipos de excepción en un solo `errors.py` están bien: es el "hogar único" que `docs/ERRORS.md` documenta. Las 157 re-exportaciones de `rates_engine/__init__.py` también: son la API estable y se conservan todas.

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

**La única excepción declarada:** `instruments -> curves` (misma capa). Los instrumentos lineales proyectan sus flujos desde un `CurveSet`. `test_layering` exige que la excepción siga siendo necesaria, así que desaparece de la lista en el mismo commit que la resuelve (§8.2).

---

## 4. Puntos de extensión

| Para añadir… | Se toca | No se toca |
|---|---|---|
| Una moneda (EUR) | `Currency` en `core.money`, su `RateIndex` y su entrada en `COLLATERAL_INDEX` (`conventions/indices.py`), un calendario si es nuevo. `tests/test_indices.py` falla hasta que la moneda dice a qué tasa descuenta | `curves` (`bootstrap_discount_curve(currency=...)` ya es genérico), `pricing`, `risk`, `app` |
| Una convención no verificada | Una entrada en el `unresolved` del índice y su porqué | Nada: la degradación llega sola a cada precio |
| Un producto lineal | Un dataclass en `instruments` con `cashflows(curve_set)` | `risk` (bumpea curvas, no productos), `hedging` |
| Una fórmula de opción | Un módulo de funciones en `models` | `volatility`, `instruments` |
| Una medida de riesgo | Una función sobre `risk.bumps.repriced` | `pricing`, `curves` |
| Una interfaz (HTTP, notebook) | Un adaptador que llama a `app.commands.COMMANDS` | `cli`, `mcp_server` |

---

## 5. Eficiencia

`benchmarks/bench_core.py`, Python 3.14, Windows, mejor de 5 corridas, ms por llamada:

| Caso | v0.3.0 | v0.4 | Mejora |
|---|---:|---:|---:|
| bootstrap 40 trimestres, log-lineal | 7.53 | 2.45 | ×3.1 |
| bootstrap 40 trimestres, monotone convex | 22.28 | 9.61 | ×2.3 |
| PV swap OIS 10 años | 0.634 | 0.066 | ×9.6 |
| PV swap OIS 10 años, monotone convex | 1.768 | 0.083 | ×21.2 |
| DV01 swap 10 años | 1.354 | 0.152 | ×8.9 |
| key rate DV01, 10 tenores | 14.20 | 2.56 | ×5.5 |
| strip hedge 2 años, 8 trimestres | 16.02 | 8.71 | ×1.8 |
| shock table, 8 choques | 2.61 | 0.73 | ×3.6 |

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
| Suite completa | 1612 → **1679** tests en verde (los nuevos: capas, caché, índices, docstrings de la capa `app`) |
| Criterios de aceptación (`scripts/audit_acceptance.py`) | 109 cubiertos, 0 sin cubrir, 5 parciales (los mismos 5 que dependen de números publicados por CME) |
| Equivalencia numérica contra v0.3.0 | Bit a bit idéntica tras cada fase |
| Payloads de la CLI contra v0.3.0 | `bootstrap` idéntico byte a byte. Cambios intencionales, todos en texto de evidencia: `float_index` de `OISSwap` pasa a `compounded_overnight`; la nota del bucketed delta apunta a `rates_engine.key_rate_dv01`; `describe` lista la nueva convención MXN `mxn_collateral_rate` |
| `ruff`, `mypy` | Limpios (72 archivos) |

---

## 7. Fases ejecutadas

| Fase | Commit | Qué |
|---|---|---|
| 1 | `730184c` | Paquetes por capa, `fx` disuelto, `__init__` sin re-exportaciones, `test_layering` por capas |
| 2 | `1208398` | Caché y `bisect` en curvas, prefijos en monotone convex, schedule en caché |
| 3 | `311bcfb` | Capa `app`; `mcp_server` deja de importar `cli` |
| 4 | `fc79148` | `conventions.indices`, `pricing.collateral`; la evidencia MXN deja de decir SOFR |
| 5 | `b7ca83c` | `risk.bumps` como primitiva única; `dv01` a `risk`; griegas a `risk.greeks` |

---

## 8. Decisiones abiertas

Las dos revierten o amplían decisiones de diseño que tomó Alan, y por eso no se ejecutaron.

### 8.1 ¿`curves` puede construir sus nodos desde instrumentos (D3)?

Hoy `ParSwapNode` repite la matemática de anualidad y par de `OISSwap`, y un swap TIIE solo existe como nodo de calibración. El diseño original prohíbe a propósito que `curves` conozca productos (`test_the_curve_layer_does_not_know_about_products`).

a) No. Los nodos se construyen desde instrumentos en `pricing` (`pricing.calibration.node_for(swap, quote)`), así que `curves` sigue sin conocer productos y la duplicación desaparece igual. **Recomendado**: conserva la regla original y elimina la duplicación.
b) Sí, al estilo QuantLib (`RateHelper` con referencia al instrumento). Menos código, pero la curva queda acoplada a los productos.

Lo que decide: si alguna vez se calibrará una curva sin tener la capa de instrumentos disponible. Si la respuesta es no, (b) es más simple.

### 8.2 ¿Los instrumentos llevan su índice (D2 y D4)?

Hoy un `OISSwap` toma su moneda de la curva donde se valúa, por decisión explícita del deep review de v0.3 (`TestARealInstrumentNotJustAProbe`). La consecuencia es que conserva su propio calendario: un `OISSwap` valuado sobre una curva MXN rueda con feriados SIFMA salvo que se pase `calendar=BMV`. La evidencia ya no miente (fase 4), pero el calendario puede estar mal sin que nada lo diga.

a) Sí. `OISSwap(index=TIIE_FONDEO, ...)` toma moneda, calendario y day count del índice; `pricing` proyecta los flujos (`pricing.project(instrument, curve_set)`) y valuar un instrumento sobre una curva de otra moneda se rechaza. Desaparece la excepción `instruments -> curves`. Es un cambio **Changed** de API y revierte la decisión del deep review. **Recomendado**: es lo que hace que agregar EUR no deje ninguna convención cableada.
b) No. Se documenta que el calendario de un instrumento no-USD se pasa a mano, y se añade una degradación cuando el calendario del instrumento no coincide con el del índice de colateral de la curva. Menos cambio, pero el acoplamiento se queda.

Lo que decide: si habrá una tercera moneda. Con solo USD y MXN (los Non-Goals del PRD-003), (b) basta.
