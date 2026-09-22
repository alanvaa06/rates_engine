"""Sensitivities: DV01, key rates, durations, convexity and greeks (layer 5).

Every measure is a bump of the curve set and a full reprice through
:mod:`rates_engine.pricing`, so a risk number can never disagree with the
price it differentiates. Depends on :mod:`rates_engine.pricing` and below.
"""
