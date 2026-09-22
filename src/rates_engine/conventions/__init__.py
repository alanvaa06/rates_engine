"""How the market counts and names things (layer 1).

Day counts, calendars, rolls, IMM dates and schedules, plus the shared
vocabulary the layers above agree on: which side of a swap, which kind of
option, how a currency pair is quoted. Every convention is an explicit type,
so an unsupported one is a refusal at construction time rather than a
plausible number later. Depends on :mod:`rates_engine.core` only.
"""
