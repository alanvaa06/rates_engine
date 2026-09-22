"""Volatility as a market object (layer 3).

A volatility that carries its units, the SABR-filled swaption cube and the
vanna-volga FX smile. The formulas these objects evaluate live in
:mod:`rates_engine.models`; this package holds the quoted data and the
calibrations built on it. Depends on :mod:`rates_engine.models` and below.
"""
