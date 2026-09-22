"""Adapters that fill a :class:`~rates_engine.market.snapshot.MarketSnapshot`.

``file`` is the offline one every test uses. ``fred`` and ``banxico`` touch
the network, and both do so inside the call, never at import. Reaching for
one should be a deliberate import from its own module, because it is the
moment a calculation stops being reproducible offline.
"""
