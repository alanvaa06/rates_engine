"""Hedging: futures strips, FX hedge structures and hedging policy (layer 6).

``futures_strip`` sizes an SR3 strip against a swap and shocks the hedged
position, ``fx_structures`` compares FX hedge structures without
recommending one, and ``program`` audits a proposed hedge against a policy
held as data. Depends on :mod:`rates_engine.risk` and below.
"""
