"""Product terms: swaps, futures, FRAs, caps and floors, swaptions (layer 3).

Immutable dataclasses describing what a trade is. Depends on
:mod:`rates_engine.conventions` and below. Linear products still project
their own floating cashflows from a curve set, which is the one declared
exception in ``tests/test_layering.py`` (``instruments -> curves``) and is
scheduled to move into :mod:`rates_engine.pricing`.
"""
