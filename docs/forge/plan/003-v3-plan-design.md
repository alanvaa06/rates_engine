# Plan-design PRD-003 (v0.3): moneda explícita, curva MXN, FX y coberturas

> Escrito 2026-09-18, después del research gate
> (`docs/forge/research/003-mxn-conventions.md`) que las Constraints del PRD
> exigen antes de este documento.

## 0. Lo que este plan decide

1. **La moneda entra primero y entra en el tipo.** Antes de una línea de
   código MXN. Es la decisión 3 del PRD y la única que toca v1 y v2.
2. **Lo verificable y lo no verificable se construyen igual de bien, y se
   marcan distinto.** El research separó los AC en dos grupos; el código no
   los separa en calidad, los separa en `unresolved_conventions`.
3. **El calendario se llama BMV porque es el de la BMV.** No se llama
   Banxico hasta que alguien lo haya comparado con la lista de Banxico.
4. **No hay default de convención de delta.** El research no lo resolvió,
   así que `DeltaConventionError` es la respuesta, que es exactamente lo
   que AC-3.4 pide.

## 1. Moneda: el cambio aditivo (fase 0)

El motor no tiene concepto de moneda. Con MXN, descontar flujos en pesos
sobre la curva USD devuelve un número y no levanta nada.

```python
class Currency(StrEnum):
    USD = "USD"
    MXN = "MXN"
```

Dónde se pone, y qué se rehúsa:

| Dónde | Campo | Qué se rompe sin él |
| --- | --- | --- |
| `DiscountCurve` | `currency: Currency = Currency.USD` | Descontar pesos en la curva de dólares |
| `Cashflow` | `currency: Currency = Currency.USD` | Sumar un flujo en pesos con uno en dólares |
| `CurveSet` | derivado de sus curvas; se rehúsa si discrepan | Una curva de proyección en otra moneda que la de descuento |
| `PriceResult` | `currency` en el payload | Un PV sin unidad monetaria |

Un `CurrencyMismatchError(RatesEngineError)`, exit code 2: la operación es
imposible, no es un input mal formado. `pricing._pv_of` lo levanta si un
flujo no coincide con la curva que lo descuenta.

**El default USD es lo que hace el cambio aditivo.** Toda llamada de v1 y
v2 sigue funcionando sin tocarse, que es la Constraint "ninguna curva USD
cambia". El test de regresión es la suite existente: 971 tests que no se
modifican.

## 2. Árbol nuevo

```
src/rates_engine/
  money.py                     # Currency, CurrencyMismatchError re-export
  conventions/calendar.py      # + BMVCalendar (existente: SIFMAUSCalendar)
  market/providers/banxico.py  # proveedor SIE, extra `data`, token del usuario
  curves/mxn.py                # TIIEFixingNode, TIIEParSwapNode, benchmark
  fx/
    __init__.py
    quote.py                   # FXQuote, convención de cotización, pips
    forward.py                 # CIP + basis, inversión a basis implícito
    delta.py                   # DeltaConvention, strike<->delta
    garman_kohlhagen.py        # precio, griegas
    vannavolga.py              # smile desde ATM/RR/BF
  hedging_structures.py        # compare_structures y las siete estructuras
```

Capas (extiende `tests/test_layering.py`): `money` justo después de
`evidence`; `fx` entre `volatility` y `curves`... **no**: `fx.forward`
necesita curvas. Orden correcto:

```
errors, evidence, money, results, conventions, market, convexity,
volatility, curves, fx, instruments, pricing, optionpricing, risk,
hedging, hedging_structures, diagnostics, reporting, cli, mcp_server
```

`fx.garman_kohlhagen`, `fx.delta` y `fx.vannavolga` no necesitan curvas y
podrían vivir en `volatility`; se quedan en `fx` porque la convención de
delta es FX y no de tasas, y mezclarlas invitaría a usar una en la otra.
`fx.forward` sí usa `curves`, y por eso `fx` va después.

## 3. Fases, en orden

| # | Fase | AC | Depende de |
| --- | --- | --- | --- |
| 0 | `Currency` en curva y cashflow, `CurrencyMismatchError` | — (decisión 3) | — |
| 1 | `BMVCalendar` | AC-1.2 | 0 |
| 2 | Garman-Kohlhagen y sus griegas | AC-3.1, AC-3.5 | — |
| 3 | `DeltaConvention`, strike↔delta, la refusal | AC-3.3, AC-3.4 | 2 |
| 4 | Vanna-volga | AC-3.2 | 2, 3 |
| 5 | `FXQuote`, forward CIP, basis, inversión, plausibilidad | AC-2.1–2.4 | 0, 2 |
| 6 | Curva MXN: nodos TIIE, benchmark, `unresolved_conventions` | AC-1.1, 1.3, 1.4 | 0, 1 |
| 7 | Comparador de estructuras | AC-4.1–4.5 | 2, 3, 4, 5 |
| 8 | Programa de cobertura en config | AC-5.1, 5.2 | 7 |
| 9 | Proveedor Banxico (extra `data`) | — (soporta 6) | 6 |
| 10 | CLI, MCP, describe, docs, CHANGELOG | — | todo |

Las fases 2-5 y 7 no dependen de ninguna convención mexicana: son las que
el research declaró construibles sin reservas. La 6 es la que lleva
`unresolved_conventions` en cada resultado.

## 4. Lo que la fase 6 marca, exactamente

```python
UNRESOLVED_MXN = (
    "tiie_day_count",        # ACT/360 asumido, no verificado
    "tiie_period_days",      # 28 asumido, no verificado
    "tiie_fondeo_vs_28",     # qué convención lleva cada benchmark
    "banxico_series_ids",    # los IDs de SIE
)
```

Cada resultado de la curva MXN arrastra una `Degradation` por convención
sin resolver, con `data_quality=ASSUMED`, y `strict=True` se rehúsa antes
de calcular. No es una advertencia decorativa: `worst_quality` sube a
`ASSUMED` y cualquier precio construido encima lo hereda, que es el
mecanismo de v1 haciendo su trabajo.

## 5. Mapa AC → test

| AC | Test | Cómo se prueba sin la fuente primaria |
| --- | --- | --- |
| 1.1 | `test_mxn_curve.py` | Re-precio exacto de instrumentos construidos; la convención va marcada, no verificada |
| 1.2 | `test_bmv_calendar.py` | Las trece reglas contra fechas calculadas a mano; `[manual-check]` para Banxico |
| 1.3 | `test_mxn_curve.py` | Dos curvas, dos benchmarks, diferencia en bp en el payload; mezclar sin marca se rehúsa |
| 1.4 | `test_mxn_curve.py` | `unresolved_conventions` presente; `strict=True` levanta |
| 2.1 | `test_fx_forward.py` | CIP es identidad: `F·P_MXN = S·P_USD` a 1e-12 |
| 2.2 | `test_fx_forward.py` | `forward == cip_forward + basis_component` por construcción |
| 2.3 | `test_fx_forward.py` | Ida y vuelta: forward → basis implícito → forward, a 1e-12 |
| 2.4 | `test_fx_forward.py` | `ImplausibleInputError` fuera de ±500 bp |
| 3.1 | `test_fx_options.py` | Put-call parity a 1e-10, identidad cerrada |
| 3.2 | `test_fx_options.py` | Reproducción exacta de los tres pilares (medida a 0.00e+00 en el prototipo) |
| 3.3 | `test_fx_delta.py` | El strike cambia entre las cuatro convenciones; premium-adjusted difiere |
| 3.4 | `test_fx_delta.py` | `DeltaConventionError` sin convención declarada |
| 3.5 | `test_fx_options.py` | Griegas por bump; vanna y volga contra sus definiciones analíticas |
| 4.1 | `test_hedge_structures.py` | La tabla tiene las cinco columnas por estructura |
| 4.2 | `test_hedge_structures.py` | Orden de primas; collar cero-costo a 1e-6 con el call resuelto |
| 4.3 | `test_hedge_structures.py` | Combinación lineal en h; descomposición de varianza |
| 4.4 | `test_hedge_structures.py` | Ningún campo de texto libre en el payload |
| 4.5 | `test_hedge_structures.py` | Escaneo de `__all__`: ningún nombre contiene `recommend` |
| 5.1 | `test_hedge_program.py` | Key desconocida → `ConfigurationError` nombrándola |
| 5.2 | `test_hedge_program.py` | Violación con `severity` y `suggestion`; `strict` se rehúsa |
| dec. 3 | `test_currency.py` | Sumar monedas distintas se rehúsa; la suite de v1/v2 pasa sin tocarse |

## 6. Inventario de fixtures

Todos construidos, todos con procedencia, todos con la misma regla que en
v2: **generados desde una forma que el modelo ajustado no puede
reproducir**, o desde una identidad que el código no usa para calcular.

| Fixture | Contenido | Generado desde |
| --- | --- | --- |
| `mxn_tiie_curve.csv` | Fixings y swaps par TIIE | Una curva conocida, con los precios redondeados al tick |
| `usdmxn_fx.csv` | Spot, puntos forward, basis por plazo | CIP más un basis conocido, redondeado a pips |
| `usdmxn_vol_smile.csv` | ATM, RR25, BF25, RR10, BF10 por plazo | Niveles típicos de USD/MXN, construidos |
| `bmv_holidays.csv` | El calendario BMV que este paquete implementa | Volcado del propio código, para revisión humana — igual que `sifma_holidays.csv` |

`bmv_holidays.csv` existe para que una persona lo compare con Banxico. Su
procedencia dirá que el `[manual-check]` está pendiente.

## 7. Riesgos de ejecución

- **El refactor de moneda toca 971 tests.** Mitigación: default USD, y la
  suite existente se ejecuta sin modificarse como test de regresión. Si un
  test de v1 necesita cambiar, el cambio no es aditivo y hay que parar.
- **`unresolved_conventions` puede volverse decorativo.** Mitigación: un
  test que construya una curva MXN con `strict=True` y exija que se
  rehúse, y otro que verifique que `worst_quality` de un precio encima de
  ella es `ASSUMED`.
- **El comparador de estructuras invita a recomendar.** Mitigación: AC-4.5
  ya es un test de superficie; se añade que el payload no tenga campos de
  texto libre.
- **Banxico sigue a 403.** El proveedor se escribe y se prueba contra un
  fixture, como `fred.py` en v1, que tampoco pudo alcanzar FRED.

## 8. Preguntas para Alan

Ninguna bloqueante. Dos para cuando tenga un navegador media hora:
las series SIE de TIIE 28 y TIIE de Fondeo con su day count, y la lista de
calendario bancario de Banxico 2026. Con eso, `unresolved_conventions` se
vacía y AC-1.1 pasa de "marcado" a "verificado" sin cambiar código.
