"""Term structures: discount curves, their calibration and their views (layer 3).

Bootstrap and dual-curve calibration, parametric curves, interpolation, and
the zero, par and forward views. A curve is calibrated from calibration
nodes that carry dates and year fractions rather than products, so this
package does not import :mod:`rates_engine.instruments`. Depends on
:mod:`rates_engine.models`, :mod:`rates_engine.market` and below.
"""
