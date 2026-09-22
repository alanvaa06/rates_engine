"""Product terms: swaps, futures, FRAs, caps and floors, swaptions (layer 3).

Immutable dataclasses describing what a trade is -- dates, rates, a side and
the :class:`~rates_engine.conventions.indices.RateIndex` it floats on, which
fixes its currency and default calendar. Nothing here reads a curve:
projecting cashflows is :mod:`rates_engine.pricing.projection`'s job.
Depends on :mod:`rates_engine.conventions` and :mod:`rates_engine.market`.
"""
