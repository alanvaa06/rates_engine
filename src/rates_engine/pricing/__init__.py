"""Valuation: present value, par rates, annuities, options, FX forwards (layer 4).

``linear`` discounts cashflows, ``options`` values swaptions, caps and floors
through the model their volatility's units select, and ``fx_forward``
applies covered interest parity with the basis kept separate. Depends on
:mod:`rates_engine.curves`, :mod:`rates_engine.volatility`,
:mod:`rates_engine.instruments` and below.
"""
