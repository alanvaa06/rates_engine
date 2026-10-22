# `rateng viz`: one HTML page from the engine's payloads

Date: 2026-09-24. Status: approved design, not yet implemented.

## What it is

A `viz` subcommand of `rateng` that runs the compute commands on a config,
renders their payloads into a single self-contained HTML page, and writes
that page to a path. The page draws the curve views, the interpolation gap,
key-rate DV01, the futures-strip hedge and its shock table, the FX forward
with its basis, and the hedge-structure payoffs. Every number on the page is
a field of a payload the CLI already prints; the page recomputes nothing.

No pip extra. The page needs the standard library and one HTML file shipped
as package data. An extra with no requirements would be an empty promise, and
the repo's own rule is that a core install runs every command.

## What it is not

- Not a command in `app.commands.COMMANDS`. A `Command` maps one config to
  one JSON dict and the MCP server mirrors that table byte for byte. `viz`
  takes two configs and returns HTML, so it is registered on the CLI parser
  directly and `rateng-mcp` does not serve it.
- Not a static image exporter. HTML only.
- Not a new package. It slots into `reporting`, `app` and `cli`, which are
  the layers that already own serialisation, use cases and the parser.
- Not listed by `describe`, which enumerates compute commands.

## Layers and files

| Layer | File | Responsibility |
| --- | --- | --- |
| 7 `reporting` | `reporting/viz.py`, `reporting/viz.html` | `render_page(payloads) -> str`. Injects the payload mapping into the template. |
| 8 `app` | `app/commands.py` | `viz(config, fx_config=None) -> tuple[str, dict]`. Runs the commands, computes the interpolation gap, calls the renderer, returns HTML and a receipt. |
| 9 `cli` | `cli.py` | The `viz` subparser, file write, receipt to stdout, narration to stderr. |

`reporting` imports nothing new. `app` already imports `curves`, which is
where `compare_interpolations` lives. `cli` already imports `app`. The
layering test passes without edits.

## `reporting.viz.render_page`

```python
def render_page(payloads: Mapping[str, Mapping[str, Any] | None]) -> str
```

`payloads` holds up to seven keys: `bootstrap`, `price`, `hedge`, `interp`,
`fx`, `structures`, `meta`. A missing or `None` key means the panel is absent
and the page shows its empty state with the file name that would fill it.

The template holds exactly one marker, `/*__DATA__*/null`. The function
replaces it with the mapping serialised by `reporting.payloads.dumps`, so
non-finite floats are handled the same way the CLI handles them, then
escapes every `</` as `<\/` so no payload string can close the script tag.
Nothing else in the template is touched by Python.

The template is read with `importlib.resources.files("rates_engine.reporting") / "viz.html"`.
`pyproject.toml` lists `reporting/*.html` under `package-data`.

## `app.commands.viz`

```python
def viz(config: dict[str, Any] | None, fx_config: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]
```

1. `require_config(config, "viz")`.
2. `bootstrap`, `price`, `hedge` from `COMMANDS` on `config`. Any refusal
   raises exactly as it would from that command.
3. If `fx_config` is given: `fx-forward` and `hedge-structures` on it. If
   not, both panels are absent.
4. The interpolation gap. Build the calibration nodes with
   `app.builders.calibration_nodes(config, as_of)` and call
   `curves.comparison.compare_interpolations(as_of, nodes, step_days=7,
   long_end_source=config["curve"].get("long_end_source"))`, the same
   pass-through `bootstrap` uses. Sample `instantaneous_forward` on both
   curves at `sample_dates`. Payload:

   ```json
   {"kind": "interpolation_comparison", "as_of": "...",
    "dates": [...], "log_linear": [...], "monotone_convex": [...], "diff_bp": [...],
    "max_residual_bp": 0.0, "max_forward_difference_bp": 4.88,
    "max_forward_difference_date": "...", "max_df_difference": 1e-7}
   ```

   `compare_interpolations` can raise `CurveArbitrageError` when monotone
   convex goes non-monotone on nodes that log-linear accepted. That failure
   is reported, not raised: the payload becomes
   `{"kind": "interpolation_comparison", "error": {"type": "CurveArbitrageError", "message": "..."}}`,
   the page renders the refusal in the panel, and the receipt says so.
   `BootstrapResidualError` from the same call is treated the same way. Any
   other exception propagates.
5. `meta`: `{"engine_version": rates_engine.__version__, "generated_at": <UTC ISO timestamp>, "as_of": config["as_of"]}`.
6. Return `(render_page(payloads), receipt)`.

The receipt is what the CLI prints:

```json
{"schema_version": "1.0", "result_type": "VizResult", "path": null, "bytes": 162613,
 "panels": {"bootstrap": "ok", "price": "ok", "hedge": "ok", "interp": "ok",
            "fx": "absent", "structures": "absent"}}
```

Panel states are `ok`, `absent`, or `reported: <ExceptionType>`. `path` is
`None` here; the CLI fills it after writing.

## The CLI

```bash
rateng viz --config c.json [--fx-config fx.json] --out desk.html [--json]
```

- `--config` and `--out` are required. `--fx-config` is optional.
- The subparser is added after the `COMMANDS` loop in `build_parser`, so the
  loop stays a faithful map of the table.
- `main` dispatches `viz` before the table lookup. It loads both configs
  with `load_config`, calls `app.commands.viz`, writes the HTML with UTF-8,
  fills `path` and `bytes` in the receipt, prints the receipt to stdout as
  one JSON document, and narrates `viz: ok -> <path>` to stderr unless
  `--json`.
- A refusal from any inner command produces the same error payload and exit
  code the command itself would produce, and no file is written. Exit 1 for
  unusable inputs, 2 for an impossible calculation, as everywhere else.
- Console output is ASCII only.

## The template

`reporting/viz.html` is the working page from this session with these
changes:

- A full `<!doctype html>` skeleton with `<meta charset>` and viewport; it
  opens as a standalone file.
- The Google Fonts link is removed. System sans and monospace stacks only.
  The page makes no network request.
- The loaded-file chip reads `embedded` instead of `demo`.
- A panel whose payload carries `error` renders the exception type and
  message where the chart would be.
- The footer stamps `meta.engine_version` and `meta.generated_at`.
- The loader button stays: it classifies dropped files by `result_type` and
  replaces the matching panel, so payloads from `rateng-mcp` or another
  machine still fit.
- The `key_rate_dv01` tenor lookup matches keys numerically (`1.0` is
  serialised as `"1.0"`, and `String(1.0)` in JavaScript is `"1"`).

Charts are hand-drawn SVG: hover crosshair and tooltip, arrow-key
navigation, a chart/table toggle on every card, a legend wherever two or
more series share an axis, light and dark palettes. The categorical palette
is the three-slot set validated in this session in both modes.

## Tests, `tests/test_viz.py`

The config is built inline; tests may not import other test modules.

1. `render_page` round-trips: the JSON between `const DATA = ` and the next
   `;` parses back equal to the input.
2. A payload string containing `</script>` yields a document with exactly
   one `</script>` tag.
3. The template resolves through `importlib.resources` and holds the marker
   exactly once.
4. `rateng viz --config c.json --out p --json`: exit 0, file exists, stdout
   is one JSON document, `fx` and `structures` are `absent`.
5. With `--fx-config`: all six panels `ok`, and `"recommend"` appears nowhere
   in the HTML, mirroring the rule `test_cli_v3` enforces on the payload.
6. A config without `swap`: exit 1, error payload, no file at `--out`.
7. `compare_interpolations` monkeypatched to raise `CurveArbitrageError`:
   exit 0, receipt panel `reported: CurveArbitrageError`, the page holds the
   message.
8. Without `--json`: stderr contains `viz: ok`, and is ASCII.

Layering, the AGENTS.md package map and the import-side-effect tests need
no edits and must stay green.

## Docs

- `AGENTS.md`: the `viz` line in the CLI block; the `reporting`, `app` and
  `cli` map rows mention it; one "will bite you" paragraph saying `viz` is
  not in `COMMANDS` and the MCP server does not serve it.
- `README.md` and `llms.txt`: one line each.
- `CHANGELOG.md`: `Unreleased`, under Added.
- `docs/ERRORS.md`: the interp failure joins the reported-not-raised list.

## Out of scope

`describe` unchanged. No version bump. No static image export. No
`compare-interpolations` command; if the gap deserves a JSON surface of its
own, that is a separate change with its own payload contract.
