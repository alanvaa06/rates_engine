# Plan-design PRD-001: v1 — curvas SOFR, futuros, swaps y cobertura

> Derivado de `docs/forge/prd/001-v1-curves-futures-swaps.md` (decisiones A1, B2, C1, D1, E, F cerradas) y de `docs/design/2026-09-16-rates-engine-design.md`. Convenciones de repo leídas directamente de `alanvaa06/optimization_engine` v0.7.0 (pyproject, CI, AGENTS.md, llms.txt, docs/ERRORS.md, tests de contrato), clonado y leído 2026-09-16 — no inferidas.
>
> **Estado: borrador para aprobación de Alan antes de `forge-run`.** Decisiones que tomé yo dentro del plan van marcadas **[decisión de plan]**; requieren visto bueno o corrección.
>
> 67 ACs en PRD-001. Este documento los mapea a 14 fases, 13 módulos y 25 archivos de test.

---

## 0. Lo que este plan decide

El PRD dice *qué* debe ser verdad. Este plan fija *dónde vive cada cosa*, *en qué orden se construye* y *qué test la prueba*. Tres decisiones estructurales gobiernan el resto:

1. **`Evidence` es un tipo único y componible, y se construye en la fase 2** — antes de que exista una sola curva. AC-9.7/9.8 lo exigen y retrofitearlo después de `hedging` es el error caro que este plan evita por construcción (§3).
2. **Los tests de contrato se escriben en la fase 0**, sobre un árbol vacío. `no_silent_swallow`, `import_side_effects`, `payloads`, `docstrings` y `typecheck_allowlist` pasan trivialmente el día 1 y se vuelven exigentes solos a medida que entra código. Escritos después, se escriben alrededor del código que deberían juzgar.
3. **La premisa de la decisión A1 se verifica antes de construir hacia ella** (§7, tarea 0.4). Si el whitepaper CME no lista la tira SR3 completa, A1 no es ejecutable y hay que saberlo en la fase 0, no en la fase 9.

---

## 1. Árbol del repo

```
rates_engine/
├── pyproject.toml              # finport-ratesengine, requires-python >=3.11
├── AGENTS.md                   # "las cosas que muerden", desde el commit 1
├── llms.txt
├── CHANGELOG.md                # Keep a Changelog
├── LICENSE                     # MIT
├── README.md                   # quickstart verificado por test
├── .github/workflows/
│   ├── ci.yml                  # lint, typecheck, test matrix, core, smoke
│   ├── docs.yml                # pdoc -> Pages
│   └── release.yml             # Trusted Publishing -> TestPyPI
├── docs/
│   ├── ERRORS.md               # contrato de refusals (AC-9.5)
│   ├── RESEARCH.md             # mapa a los artículos del wiki
│   ├── RELEASING.md
│   ├── design/ · forge/        # ya existentes
├── src/rates_engine/
│   ├── __init__.py             # superficie pública; sin side effects (AC-2.5)
│   ├── py.typed
│   ├── errors.py               # todas las excepciones, un archivo
│   ├── evidence.py             # Evidence, Provenance, DataQuality, Degradation
│   ├── conventions/
│   │   ├── daycount.py         # DayCount, year_fraction
│   │   ├── calendar.py         # Calendar (Protocol), SIFMA_US
│   │   └── schedule.py         # Schedule, imm_dates, roll
│   ├── market/
│   │   ├── snapshot.py         # MarketSnapshot, Series
│   │   └── providers/{file,fred,cme}.py
│   ├── instruments/
│   │   ├── cashflow.py         # Cashflow
│   │   ├── swaps.py            # OISSwap, IRSwap
│   │   ├── futures.py          # SOFRFuture1M, SOFRFuture3M
│   │   └── fra.py              # FRA
│   ├── curves/
│   │   ├── discount.py         # DiscountCurve, log_linear_df
│   │   ├── views.py            # zero_curve, par_curve, forward_curve
│   │   ├── bootstrap.py        # bootstrap_discount_curve
│   │   └── dual.py             # solve_dual_curve
│   ├── convexity.py            # ho_lee, hull_white, realized_sofr
│   ├── pricing.py              # pv, par_rate, annuity, dv01
│   ├── risk.py                 # US-10: key rate, duración, convexidad
│   ├── hedging.py              # strip_hedge, shock_table
│   ├── diagnostics.py          # ensamblado de evidencia
│   ├── reporting/payloads.py   # schema_version, serialización
│   └── cli.py                  # rateng
└── tests/
    ├── fixtures/               # datos congelados + .provenance.json por archivo
    └── test_*.py               # 25 archivos, §6
```

**[decisión de plan]** `conventions`, `market`, `instruments` y `curves` son subpaquetes; el resto son módulos planos. El criterio es el mismo de optengine: subpaquete cuando hay ≥ 3 responsabilidades separables, módulo cuando hay una. `risk` es un módulo aparte de `pricing` porque la atribución tiene sus propias trampas de convención (decisiones E y F) y merece su propio archivo de test.

---

## 2. Superficie pública por módulo

Firmas concretas, para que `forge-run` no las invente. Todas con anotaciones completas; todo parámetro numérico declara unidad en el docstring (AC-9.6).

### `conventions`
```python
class DayCount(Enum): ACT_360, ACT_365F, THIRTY_360
def year_fraction(start: date, end: date, dc: DayCount) -> float
class BusinessDayConvention(Enum): FOLLOWING, MODIFIED_FOLLOWING, PRECEDING
class Calendar(Protocol):
    def is_business_day(self, d: date) -> bool
    def adjust(self, d: date, conv: BusinessDayConvention) -> date
SIFMA_US: Calendar
def imm_dates(year: int) -> tuple[date, date, date, date]
@dataclass(frozen=True) class Schedule: ...   # fixing/accrual/payment dates
```

### `evidence` — el corazón del contrato (§3)
```python
class DataQuality(str, Enum): OBSERVED, SYNTHETIC, PROXY, ASSUMED
@dataclass(frozen=True) class Provenance:
    source: str; series_id: str | None; retrieved_at: datetime | None
    instrument_kind: str | None; data_quality: DataQuality
@dataclass(frozen=True) class Degradation: code: str; message: str; data_quality: DataQuality
@dataclass(frozen=True) class Evidence:
    produced_by: str
    inputs: tuple[Provenance, ...]
    fields: Mapping[str, object]
    warnings: tuple[Degradation, ...]
    sources: tuple[Evidence, ...]
    @property
    def qualities(self) -> frozenset[DataQuality]   # unión recursiva
    @property
    def worst_quality(self) -> DataQuality          # rango declarado, §3
    def to_dict(self) -> dict[str, object]          # recursivo
```

### `curves`
```python
@dataclass(frozen=True) class DiscountCurve:
    nodes: tuple[date, ...]; dfs: tuple[float, ...]; interpolation: str
    def df(self, t: date) -> float
    def zero(self, t: date, compounding: str, dc: DayCount) -> float
    def forward(self, t1: date, t2: date, dc: DayCount) -> float
def bootstrap_discount_curve(snapshot, instruments, *, interpolation="log_linear_df",
                             long_end_source: str | None = None, strict: bool = True) -> BootstrapResult
def zero_curve(curve, *, compounding, day_count) -> CurveView
def par_curve(curve, tenors, *, frequency, day_count) -> CurveView
def forward_curve(curve, tenor) -> CurveView
def solve_dual_curve(ois, tenor, basis, *, mode, inputs_origin="synthetic") -> DualCurveResult
```
`long_end_source=None` es el default y **no** admite instrumentos `DGS*`; pasarlos sin declarar `"treasury_proxy"` lanza `ProxySourceNotDeclaredError` (AC-3.6).

### `pricing` y `risk`
```python
def pv(instrument, curve_set) -> PriceResult
def par_rate(swap, curve_set) -> PriceResult
def annuity(swap, curve_set) -> PriceResult
def dv01(instrument, curve_set, *, bump_bp: float = 1.0) -> PriceResult      # paralelo (AC-6.5)

def key_rate_dv01(instrument, curve_set, key_tenors) -> RiskResult           # tent, AC-10.1
def key_rate_duration(instrument, curve_set, key_tenors) -> RiskResult       # rehúsa a PV≈0
def pvbp(instrument, curve_set) -> RiskResult                                # ≡ dv01
def money_duration(instrument, curve_set) -> RiskResult                      # = DV01 × 10_000
def money_convexity(instrument, curve_set, *, bump_bp=1.0) -> RiskResult     # definida a PV=0
def effective_duration(instrument, curve_set, *, bump_bp=1.0) -> RiskResult
def effective_convexity(instrument, curve_set, *, bump_bp=1.0) -> RiskResult
def macaulay_duration(*a, **k) -> NoReturn                                   # stub, AC-10.13
def modified_duration(*a, **k) -> NoReturn                                   # stub, AC-10.13
```

### `hedging`
```python
def strip_hedge(swap, futures_strip, curve_set) -> HedgeResult
    # HedgeResult.bucketed_delta_by_instrument, bump_basis="instrument_quote" (AC-10.6)
def shock_table(hedge, shocks_bp=(-100,-50,-25,-10,10,25,50,100)) -> ShockTableResult
```

---

## 3. `Evidence`: cómo compone, y por qué así

El riesgo que este diseño cierra es concreto: `hedge()` consume `price()` que consume `bootstrap()`. Si cada nivel aplana la evidencia del anterior en un `dict`, la marca `data_quality="proxy"` del nodo largo (decisión B2) se pierde antes de llegar al `HedgeResult`, y el usuario ve 779 contratos sin saber que descansan en un swap spread. AC-9.8 lo prohíbe; la estructura lo hace imposible.

- **`sources` es una tupla de `Evidence`, no un dict aplanado.** Cada resultado conserva la evidencia completa de sus insumos. `to_dict()` serializa recursivamente.
- **`qualities` es la unión recursiva** de las calidades de `inputs`, `warnings` y `sources`. Un `HedgeResult` construido sobre curva con proxy tiene `PROXY` en su conjunto sin que `hedging.py` sepa nada de Treasuries.
- **`worst_quality` usa un rango declarado: `OBSERVED(0) < SYNTHETIC(1) < PROXY(2) < ASSUMED(3)`.** El razonamiento, para que sea decisión y no accidente: *synthetic* es deliberado y controlado (inputs de test, US-7); *proxy* es un dato real que sustituye a otro y arrastra un sesgo conocido pero no cuantificado; *assumed* es un número que nadie verificó (el nocional SR1). El rango va de "sé exactamente qué es" a "no lo he comprobado". **[decisión de plan]** — si Alan prefiere otro orden, cambia una constante y un test.
- **`Evidence` es inmutable y se compone por construcción**, nunca mutando: `Evidence(produced_by=..., sources=(prev_evidence,))`.

Test que lo ancla (`test_evidence.py`): se construye una cadena de tres niveles con un `PROXY` sembrado en el más profundo y se afirma que aparece en el `to_dict()` del más alto, y que `worst_quality` del nivel 3 es `PROXY`. Si alguien aplana, el test cae.

---

## 4. Fases, en orden de construcción

Cada fase es TDD estricto: los tests de sus ACs se escriben y fallan antes de que exista el módulo. Una fase no cierra con tests rojos ni con ACs sin cubrir.

| # | Fase | ACs | Módulos | Notas |
|---|---|---|---|---|
| **F0** | Andamio y tests de contrato | — (DoD) | pyproject, CI, 5 tests de contrato | Incluye la **tarea 0.4**, §7 |
| **F1** | Convenciones | 1.1–1.4 | `conventions/*` | `[manual-check]` AC-1.2 |
| **F2** | Evidencia y errores | 9.7, 9.8 | `evidence.py`, `errors.py` | §3; `docs/ERRORS.md` empieza aquí y crece con cada excepción |
| **F3** | Mercado (proveedor `file`) y fixtures | 2.2–2.4, 2.5, 2.6 | `market/*` salvo `fred` | §5 |
| **F4** | Instrumentos y settlement SR1/SR3 | 5.1, 5.2, 8.5 | `instruments/*` | Primer golden contra CME |
| **F5** | Convexidad | 5.3–5.7 | `convexity.py` | Tabla Hull; consistencia κ→0 |
| **F6** | Curva de descuento y bootstrap (solo futuros) | 3.1–3.5 | `curves/{discount,bootstrap}.py` | AC-3.3 según §8 |
| **F7** | Cuatro vistas de curva | 4.1–4.5 | `curves/views.py` | **Precede a F10**: el key rate bumpea la curva cero |
| **F8** | Pricing | 6.1–6.5 | `pricing.py` | 6.3/6.4 con curva de tenor sintética |
| **F9** | Cobertura y goldens CME | 6.6, 8.1–8.4 | `hedging.py` | **Revisión independiente** |
| **F10** | Riesgo: key rate, duración, convexidad | 10.1–10.13 | `risk.py` | **Revisión independiente**; depende de F7+F8+F9 |
| **F11** | Proveedor FRED y proxy Treasury | 2.1, 3.6–3.8 | `market/providers/fred.py` | Primer código que toca red (fuera de tests) |
| **F12** | Dual-curva sintética | 7.1–7.5 | `curves/dual.py` | **Revisión independiente** |
| **F13** | CLI, payloads, documentación, release | 9.1–9.6 | `reporting/`, `cli.py`, docs | Tag `v0.1.0` → TestPyPI |

Las tres fases marcadas con revisión independiente son las que el diseño §7 señala como *heavy*: `solve_dual_curve`, `shock_table` y —añadido aquí— `risk.py`, porque una convención de duración mal puesta produce un número plausible y equivocado, que es el peor modo de falla de esta librería.

### Dependencias que importan
- **F7 antes que F10.** AC-10.1 bumpea nodos de la curva cero; sin `zero_curve` no hay dónde poner el tent.
- **F9 antes que F10.** AC-10.6/10.7 reconcilian contra `HedgeResult`; AC-10.10 contra `shock_table`.
- **F2 antes que todo lo que produzca un resultado.** Ningún módulo devuelve un número desnudo en ninguna fase.
- **F11 es tardía a propósito.** El proveedor de red no bloquea nada: los fixtures de F3 cubren todos los ACs numéricos, y así el suite entero sigue siendo offline.

---

## 5. Inventario de fixtures

Todo fixture lleva un `<nombre>.provenance.json` al lado: `source`, `url`, `retrieved_at`, `retrieved_by`, `notes`. La regla de evidencia aplica también a los datos de prueba — un fixture sin procedencia es un número inventado con más pasos.

| Fixture | Contenido | Sirve a | Obtención |
|---|---|---|---|
| `sifma_holidays.csv` | Feriados SIFMA 2018–2030 | AC-1.2 | Manual desde SIFMA; `[manual-check]` |
| `sofr_fixings.csv` | SOFR diario 2018-04 → corte | 2.3, 2.4, 5.1, 5.2, 5.5 | FRED `SOFR`, congelado |
| `cme_sr1_settlements.csv` | ≥ 6 contratos SR1 expirados con final settlement | 5.1 | CME público |
| `cme_sr3_settlements.csv` | ≥ 6 contratos SR3 expirados con final settlement | 5.2 | CME público |
| `cme_whitepaper_2025_strip.csv` | Tira SR3 que lista el whitepaper + hipótesis de reconstrucción | 6.6, 8.1, 8.2 | **Bloqueante: tarea 0.4** |
| `hull_convexity_table.csv` | 0.62 bp @1y, 2.25 bp @2y con σ=1% | 5.3 | Tabla Hull vía wiki |
| `dgs_treasury.csv` | DGS2/3/5/10 | 2.6, 3.6–3.8 | FRED, congelado |
| `synthetic_dual_curve.json` | Curvas OIS y tenor con basis conocido | 7.1–7.5 | Generado; `inputs_origin="synthetic"` |

---

## 6. Mapa AC → test

| Archivo | ACs |
|---|---|
| `test_conventions.py` | 1.1, 1.2, 1.3, 1.4 |
| `test_evidence.py` | 9.7, 9.8 |
| `test_errors_contract.py` | 9.5 |
| `test_market_file.py` | 2.2, 2.3, 2.4, 2.6 |
| `test_market_fred.py` | 2.1 |
| `test_import_side_effects.py` | 2.5 |
| `test_futures_settlement.py` | 5.1, 5.2, 8.5 |
| `test_convexity.py` | 5.3, 5.4, 5.5, 5.6, 5.7 |
| `test_bootstrap.py` | 3.1, 3.2, 3.4, 3.5 |
| `test_bootstrap_proxy.py` | 3.6, 3.7, 3.8 |
| `test_determinism.py` | 3.3 |
| `test_curve_views.py` | 4.1, 4.2, 4.3, 4.4, 4.5 |
| `test_pricing.py` | 6.1, 6.2, 6.3, 6.4, 6.5 |
| `test_hedging.py` | 8.3, 8.4 |
| `test_golden_cme.py` | 6.6, 8.1, 8.2 |
| `test_risk_key_rate.py` | 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7 |
| `test_risk_duration.py` | 10.8, 10.9, 10.10, 10.11, 10.12, 10.13 |
| `test_dual_curve.py` | 7.1, 7.2, 7.3, 7.4, 7.5 |
| `test_payloads.py` | 9.1 |
| `test_cli_json.py` | 9.2, 9.3 |
| `test_no_silent_swallow.py` | 9.4 |
| `test_docstrings.py` | 9.6 |
| `test_properties.py` | propiedades hypothesis (diseño, sección *Propiedades*) |
| `test_typecheck_allowlist.py` | DoD (ceiling) |
| `test_readme_quickstart.py` | DoD (quickstart verificado) |

Los 67 ACs de PRD-001 están cubiertos, ninguno aparece dos veces y ninguna fila mapea un AC inexistente — verificado programáticamente contra el PRD, no a ojo.

---

## 7. Tarea 0.4 — verificar la premisa de A1 antes de construir hacia ella

**Bloqueante de F9, ejecutable en F0.** La decisión A1 asume que el whitepaper CME/Rogerson 2025 lista los precios SR3 suficientes para reconstruir la curva de entrada. El diseño §9 ya advierte que "la curva exacta de entrada se reconstruye". Si el documento no trae la tira completa, A1 no es ejecutable tal como está escrita.

Procedimiento: abrir el whitepaper, extraer la tira SR3 a `cme_whitepaper_2025_strip.csv`, y anotar en su `.provenance.json` qué está publicado y qué hay que suponer (fecha de valuación, σ de convexidad, convención de stub).

Salidas posibles:
- **Tira completa** → A1 procede sin cambios.
- **Tira parcial** → se documentan las hipótesis faltantes en el fixture y se avisa a Alan **antes de F9**; la tolerancia de ±3% sigue igual, la cláusula `xfail` queda armada.
- **Sin tira** → A1 cae. Hay que reabrir la decisión, no improvisar un fixture propio en silencio (eso era la opción A2, que Alan descartó).

Hacer esto en F0 cuesta una hora. Descubrirlo en F9 cuesta nueve fases construidas hacia un golden que no existe.

### Resultado (2026-09-16): **sin tira**, por causa ambiental

El entorno de construcción tenía egress limitado a registries de paquetes y GitHub por política de la organización. `fred.stlouisfed.org` devolvió 403 en el proxy y CME era inalcanzable, así que no se pudo abrir el whitepaper ni descargar los settlements finales ni los fixings. La causa no es que el whitepaper no traiga la tira — eso sigue sin saberse — sino que no se pudo mirar.

Aplicada la cláusula de A1: los tres goldens del whitepaper y los dos de settlement CME quedan `skip` con la razón nombrando el fixture faltante, **las tolerancias no se movieron**, y se activan solos en cuanto los archivos existan. `tests/fixtures/cme_published/README.md` dice exactamente qué archivo hace falta, con qué columnas, y qué hipótesis de reconstrucción debe registrar el sidecar. Nada se sustituyó.

Que el escalamiento llegara en F0 y no en F9 es el valor que esta tarea tenía que entregar, y lo entregó.

---

## 8. Determinismo, en concreto (AC-3.3)

Dos afirmaciones distintas, dos tests distintos:
- **Dentro de un intérprete:** `bootstrap` dos veces sobre la misma entrada da DFs bit-a-bit idénticos. Test directo de igualdad.
- **Entre celdas de la matriz:** 1e-14 relativo. Se implementa congelando un `.npz` de DFs de referencia generado en la celda 3.11/Linux y comparando con `np.allclose(rtol=1e-14, atol=0)` en las demás.

Requisitos de implementación que lo sostienen: orden de iteración estable (tuplas, no sets); sin `dict` ordenado por hash en rutas numéricas; `scipy.optimize.least_squares` con `x0` determinista y tolerancias explícitas; ninguna llamada a RNG en `src/`.

---

## 9. Empaquetado y CI (espejo verificado de optengine)

```toml
[project]
name = "finport-ratesengine"
requires-python = ">=3.11"
dependencies = ["numpy>=2.0", "pandas>=2.2", "scipy>=1.11"]
[project.optional-dependencies]
data = ["pyarrow>=14.0"]
dev  = ["pytest>=7.4", "pytest-cov>=4.1", "hypothesis>=6.100", "ruff>=0.5",
        "mypy>=1.11", "pandas-stubs>=2.2"]
docs = ["pdoc>=14.0"]
[project.scripts]
rateng = "rates_engine.cli:main"
[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "W"]
ignore = ["E501"]
[tool.mypy]
python_version = "3.11"
warn_unused_ignores = true
no_implicit_optional = true
ignore_missing_imports = true
```

**El allowlist de mypy arranca en cero y el ceiling de `test_typecheck_allowlist.py` es 0.** optengine carga 45 módulos diferidos porque es deuda heredada; este repo es nuevo y no tiene excusa para nacer con deuda. El test conserva la misma mecánica (el ceiling solo baja, y una entrada que nombre un módulo inexistente falla).

Jobs de CI, siguiendo el reparto de optengine:
- `lint` — ruff sobre `src tests`, sin instalar el proyecto.
- `typecheck` — mypy en 3.11, con el reporte del allowlist en el step summary.
- `test` — matriz **3.11, 3.12, 3.13 en Linux + una celda 3.12 en Windows**. La celda Windows no es cosmética: Alan desarrolla en win32 y la constraint de salida ASCII y rutas se rompe ahí o en ningún lado.
- `core` — `pip install .` sin extras, verifica que ninguna extra se coló en una ruta de import y que el motor bootstrapea y cubre.
- `smoke` — ejerce el console script instalado: `rateng describe`, `rateng bootstrap --json`, `rateng hedge --json`.

Sin jobs `extras` ni `ui`: v1 no tiene MCP (fase 2) ni app.

---

## 10. Riesgos de ejecución

| Riesgo | Fase | Mitigación |
|---|---|---|
| El whitepaper no trae la tira SR3 | F0 | Tarea 0.4; se escala a Alan antes de F9 |
| Los settlements finales CME de contratos expirados no son descargables sin cuenta | F4 | Alternativa: reconstruir el settlement desde fixings FRED y comparar contra los pocos valores que el wiki cita; si se degrada, el AC baja de "vs CME" a "vs fixings" y se anota en CHANGELOG |
| La lista SIFMA se carga mal | F1 | `[manual-check]` explícito + el golden de settlements la valida indirectamente |
| El solver dual no converge con nodos densos | F12 | `UnderdeterminedCurveError` ya contratado (AC-7.4); reportar condición del jacobiano en evidencia |
| `money_convexity` no reconcilia con `shock_table` al 10% (AC-10.10) | F10 | Es el test haciendo su trabajo: significa que una de las dos está mal. No se afloja la tolerancia sin entender cuál |

---

## 11. Preguntas para Alan antes de `forge-run`

1. **Superficie CLI para US-10.** El diseño §3.8 fija cuatro comandos (`bootstrap`, `price`, `hedge`, `describe`) y US-10 no existía entonces. **[decisión de plan]**: las medidas de riesgo salen dentro del payload de `rateng price`, sin comando nuevo. Si prefieres un `rateng risk` propio, lo digo ahora y cambia el plan de F13, no después.
2. **Rango de `DataQuality`** (§3): `OBSERVED < SYNTHETIC < PROXY < ASSUMED`. ¿Te cuadra ese orden?
3. **Ceiling 0 del allowlist de mypy** (§9): significa que ningún módulo puede entrar con errores de tipo diferidos. Es más estricto que optengine hoy. ¿Lo sostenemos?
