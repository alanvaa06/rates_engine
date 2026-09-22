"""Closed-form mathematics, parameters in and numbers out (layer 2).

Black, Bachelier, Garman-Kohlhagen, SABR, the FX delta conventions, the
futures convexity adjustment and the Gaussian they share. A model takes its
inputs as given -- a forward, a strike, a volatility -- and never builds
them: finding the forward is :mod:`rates_engine.pricing`'s job and measuring
a volatility is :mod:`rates_engine.market`'s. Depends on
:mod:`rates_engine.conventions` and below.
"""
