"""What was observed, and where it came from (layer 2).

Series and snapshots with per-series provenance, the SOFR compounding and
averaging rules, statistics estimated from history, and the providers that
fill a snapshot. ``providers.file`` is offline; ``fred`` and ``banxico``
touch the network inside the call and never at import. Depends on
:mod:`rates_engine.conventions` and below; knows nothing about curves.
"""
