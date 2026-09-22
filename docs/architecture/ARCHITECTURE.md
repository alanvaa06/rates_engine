# Arquitectura de `rates_engine` (reorganización v0.4)

*Escrito 2026-09-22 sobre `main` @ `cf3c2f1` (v0.3.0, 60 módulos, 14.9k líneas, 1612 tests). Rama: `refactor/architecture`.*

Este documento reemplaza la sección de módulos de `docs/design/2026-09-16-rates-engine-design.md`, que describía la fase 1 antes de que existiera código. Los PRDs siguen vigentes: esta reorganización no cambia ningún número ni ninguna refusal documentada.

---

## 1. Diagnóstico: qué estaba mal y cómo se verificó

La calidad por función es alta: evidencia encadenada, refusals nombradas, 1612 tests. Los problemas están en la **estructura**, y cada uno se comprobó en el código, no se infirió.

| # | Problema | Evidencia | Costo |
|---|---|---|---|
| D1 | No hay capas, solo un orden topológico | `tests/test_layering.py` ordenaba 21 módulos en una lista total: `fx` quedaba *debajo* de `instruments` y `convexity` debajo de `volatility` sin que eso significara nada. 14 módulos sueltos en la raíz junto a 7 paquetes | Cada módulo nuevo exige elegir un rango arbitrario. La prueba garantiza que no hay ciclos, pero no que haya fronteras |
| D2 | La moneda es una etiqueta; índice y calendario están cableados a USD | `calendar: SIFMAUSCalendar = SIFMA_US` (tipo concreto, no el `Calendar` Protocol) en swaps, capfloor, snapshot, views y parametric. `pricing`, `optionpricing` y `convexity` escriben `"discounting": "collateral_rate_ois_sofr"` en la evidencia de **cualquier** curva, incluida la MXN | Un swap en pesos sale con evidencia que dice "descontado en OIS-SOFR" y con índice `compounded_sofr`: **evidencia falsa**, justo lo que el principio del proyecto prohíbe. Una tercera moneda obliga a otro `curves/mxn.py` |
| D3 | Dos vocabularios de instrumentos | `instruments.OISSwap` (para valuar) frente a `curves.bootstrap.ParSwapNode` (para calibrar), con su propia `annuity`/`par_rate`. El swap TIIE solo existe como nodo (`tiie_par_swap_node`) y no se puede valuar como instrumento | La matemática de reprecio está duplicada, y un producto calibrable no es necesariamente valuable |
| D4 | Los instrumentos valúan | `OISSwap.float_cashflows(curve_set)` proyecta desde la curva. `OISSwap` e `IRSwap` duplican la pata fija línea por línea. `Schedule.generate` corre en **cada** llamada a `cashflows`, así que cada bump de riesgo recalcula el calendario | Términos del producto acoplados al modelo de valuación. Trabajo repetido en el camino caliente |
| D5 | La valuación y el riesgo están fragmentados | `pricing.py` (lineales), `optionpricing.py` (swaptions, caps), `fx/forward.py`, `fx/garman_kohlhagen.py`. `dv01` vive en `pricing` y el resto del riesgo en `risk`. `hedging.py` tiene su propio mecanismo de bump de cotizaciones | Para saber "cómo se valúa X" hay que revisar cuatro módulos, y la diferencia central está escrita varias veces |
| D6 | Fórmulas mezcladas con objetos de mercado | `volatility/` contiene fórmulas (`black`, `bachelier`, `sabr`, `_gaussian`), objetos (`Volatility`, `VolCube`) y un enum de producto (`OptionKind`). Garman-Kohlhagen vive en `fx/`. `convexity.py` mezcla la fórmula con la estimación de σ a partir de `MarketSnapshot` | No hay un lugar único para la matemática cerrada |
| D7 | No hay capa de aplicación | `cli.py` (595 líneas) parsea la config, arma objetos de dominio (`_build_instruments`, `_flat_curve`) y ejecuta los casos de uso. `mcp_server` importa el `_COMMANDS` **privado** de `cli` | Una tercera interfaz (HTTP, notebook) tendría que importar la CLI |
| D8 | "Hedging" disperso | `hedging.py` (tira SR3), `hedging_structures.py` (FX), `hedge_program.py` (política), los tres en la raíz | Tres dominios distintos con el mismo prefijo y sin paquete |
| D9 | Evaluación de curva sin caché | `DiscountCurve.df()` recalcula `node_times` (N llamadas a `year_fraction`) y `log(dfs)` en cada llamada, busca el tramo con un barrido lineal y, con `monotone_convex`, **reconstruye el interpolante en cada llamada** | Costo O(N) por `df()` en todas las rutas de pricing y riesgo |

**Qué no es un problema, aunque lo parezca.** Los 31 tipos de excepción en un solo `errors.py` están bien: es el "hogar único" que `docs/ERRORS.md` documenta. Las ~157 re-exportaciones de `rates_engine/__init__.py` también: son la API estable y se conservan.

---

## 2. Principios

1. **Las dependencias apuntan hacia abajo por capas con significado.** Un paquete solo importa de capas inferiores. Dos paquetes de la misma capa no se conocen entre sí. Lo verifica `tests/test_layering.py`.
2. **Cada eje de crecimiento tiene un único punto de extensión** (§4). Añadir una moneda, un producto, un modelo o una interfaz no toca código ajeno a ese eje.
3. **Términos ≠ valuación.** Un instrumento es un dato inmutable. Proyectar flujos, descontar y medir riesgo es trabajo de `pricing` y `risk`.
4. **La evidencia se deriva de los objetos, no de literales.** Qué curva, qué índice y qué colateral salen de la curva y del índice que se usaron.
5. **API pública estable.** `from rates_engine import X` sigue funcionando para los mismos nombres. Las rutas internas cambian (paquete 0.x, anotado en `CHANGELOG`).
6. **La eficiencia se mide, no se supone.** `benchmarks/bench_core.py` corre antes y después. Ninguna fase se integra si hace más lento un caso.

---

## 3. Capas y paquetes

```
 9  cli · mcp_server                      adaptadores: argparse / MCP → app
 8  app                                   casos de uso: config → dominio → resultado
 7  reporting                             serialización JSON de resultados y errores
 6  hedging                               tira de futuros, estructuras FX, política
 5  risk                                  bump único → DV01, key rate, duraciones, griegas
 4  pricing                               PV, par, anualidad, opciones, forward FX
 3  curves · volatility · instruments     estructuras temporales · superficies · términos
 2  market · models                       datos observados · matemática cerrada
 1  conventions                           day count, calendarios, IMM, índices, lados
 0  core                                  errors, evidence, results, money, diagnostics
```

| Paquete | Contiene | Viene de |
|---|---|---|
| `core` | `errors`, `evidence`, `results`, `money`, `diagnostics` | raíz |
| `conventions` | `daycount`, `calendar`, `schedule`, `sides` (`Side`, `OptionKind`), `currency_pair`, `indices` | `conventions`, `instruments.side`, `volatility.kinds`, `fx.quote` |
| `market` | `snapshot`, `classify`, `estimators` (σ realizada), `providers/{fred,file,banxico}` | `market`, `convexity.realized_sofr_sigma` |
| `models` | `gaussian`, `black`, `bachelier`, `garman_kohlhagen`, `fx_delta`, `sabr`, `convexity` | `volatility.*`, `fx.*`, `convexity` |
| `curves` | `discount`, `interpolation`, `bootstrap`, `dual`, `mxn`, `parametric`, `views`, `comparison` | `curves` |
| `volatility` | `units` (`Volatility`), `cube` (SABR), `fx_smile` (vanna-volga) | `volatility`, `fx.vannavolga` |
| `instruments` | `cashflow`, `swaps`, `futures`, `fra`, `capfloor`, `swaption` | `instruments` |
| `pricing` | `linear` (pv, par, anualidad, paramétrica), `options` (swaptions, caps), `fx_forward` | `pricing`, `optionpricing`, `fx.forward` |
| `risk` | `bumps` (primitiva única), `sensitivities`, `greeks` | `risk`, `pricing.dv01` |
| `hedging` | `futures_strip`, `fx_structures`, `program` | `hedging`, `hedging_structures`, `hedge_program` |
| `reporting` | `payloads` | igual |
| `app` | `config`, `builders`, `commands` | `cli` |
| `cli`, `mcp_server` | adaptadores delgados | igual (entry points sin cambio) |

**Por qué 10 capas y no 4.** Cada frontera corresponde a una pregunta distinta: ¿cómo se cuenta?, ¿qué se observó?, ¿qué forma tiene la curva?, ¿cuánto vale?, ¿cuánto se mueve?, ¿cómo se cubre? Juntar `pricing` con `risk` volvería a permitir que el riesgo reimplemente la valuación, que es justo D5.

**Por qué el vocabulario (`Side`, `OptionKind`, `CurrencyPair`) va a `conventions`.** Lo usan las fórmulas (capa 2), los productos (3) y las coberturas (6). Si viviera en `volatility` o en `instruments`, las fórmulas tendrían que importar hacia arriba.

---

## 4. Puntos de extensión

| Para añadir… | Se toca | No se toca |
|---|---|---|
| Una moneda o un índice (EUR/€STR) | `conventions/indices.py`: un `RateIndex` con moneda, day count, calendario, lag y `unresolved`; un calendario si es nuevo | `curves`, `pricing`, `risk`, `app` |
| Un producto lineal | Un dataclass en `instruments` y su proyección de flujos en `pricing/linear.py` | `risk` (bumpea curvas, no productos), `hedging` |
| Una fórmula de opción | Un módulo de funciones en `models` | `volatility`, `instruments` |
| Una medida de riesgo | Una función sobre `risk.bumps` | `pricing`, `curves` |
| Una interfaz (HTTP, notebook) | Un adaptador que llama a `app.commands` | `cli`, `mcp_server` |

---

## 5. Eficiencia

Línea base (`benchmarks/bench_core.py`, Python 3.14, mejor de 5 corridas):

| Caso | ms/llamada |
|---|---|
| bootstrap 40 trimestres, log-lineal | 7.53 |
| bootstrap 40 trimestres, monotone convex | 22.28 |
| PV swap OIS 10 años | 0.63 |
| PV swap OIS 10 años, monotone convex | 1.77 |
| DV01 swap 10 años | 1.35 |
| key rate DV01, 10 tenores | 14.20 |
| strip hedge 2 años, 8 trimestres | 16.02 |
| shock table, 8 choques | 2.61 |

Cambios que la mejoran sin alterar ningún número:
- `DiscountCurve` guarda en caché los tiempos de nodo, los log-DF y el interpolante monotone-convex (`functools.cached_property` sobre un dataclass congelado sin `slots`).
- La búsqueda de tramo pasa de un barrido lineal a `bisect`, con el mismo tramo elegido en las fronteras.
- El `Schedule` de cada instrumento se calcula una vez por instancia, no una vez por llamada.

**Regla:** los tests existentes exigen reproducibilidad bit a bit (`test_determinism`). Ninguna optimización cambia el orden de las operaciones de punto flotante.

---

## 6. Plan por fases

Cada fase deja los 1612 tests en verde y el benchmark sin regresión, y se integra en su propio commit.

| Fase | Qué | Riesgo |
|---|---|---|
| 1 | Mover módulos a los paquetes de §3, reescribir imports, nueva `test_layering` por capas y `AGENTS.md` | Bajo: mecánico, sin cambios de comportamiento |
| 2 | Caché y `bisect` en `DiscountCurve`, `Schedule` en caché | Bajo: medible, determinista |
| 3 | Capa `app`: extraer config, builders y comandos de `cli`. `mcp_server` depende de `app`, no de `cli` | Bajo: la prueba de igualdad byte a byte CLI↔MCP lo cubre |
| 4 | `RateIndex` como dato; `Calendar` Protocol en lugar de `SIFMAUSCalendar`; evidencia de descuento derivada de la curva (corrige D2) | Medio: cambia la evidencia de las valuaciones MXN, que hoy es falsa |
| 5 | `risk.bumps` como primitiva única; `dv01` pasa a `risk` | Medio: la diferencia central tiene que quedar idéntica |

**Fuera de esta reorganización, a propósito:**
- **Unificar los nodos de calibración con los instrumentos (D3).** Exige decidir si `curves` puede conocer productos, algo que el diseño original prohibió de forma explícita (`test_the_curve_layer_does_not_know_about_products`). Queda como decisión abierta (§7).
- **Sacar la proyección de flujos de los instrumentos (D4, primera mitad).** Cambia la firma pública `OISSwap.cashflows(curve_set)`. Pertenece a una v0.5 con su propia entrada **Changed**.

---

## 7. Decisiones abiertas

1. **¿`curves` puede importar `instruments` para calibrar (D3)?**
   a) No. Los nodos se construyen desde instrumentos en `pricing` (`pricing.calibration.node_for(swap, quote)`) y `curves` sigue sin conocer productos. **Recomendado**: conserva la regla del diseño original y elimina la duplicación igual.
   b) Sí, al estilo QuantLib (`RateHelper` con referencia al instrumento). Menos código, pero la curva queda acoplada a los productos.
2. **¿La proyección de flujos sale de los instrumentos en v0.5 (D4)?**
   a) Sí, con `pricing.project(instrument, curve_set)` y una entrada **Changed** en el CHANGELOG. **Recomendado**: es el cambio que hace que un producto nuevo no tenga que saber de curvas.
   b) No: se quedan como métodos y se documenta el acoplamiento.
