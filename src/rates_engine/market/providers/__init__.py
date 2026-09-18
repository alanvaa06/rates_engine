"""Providers that fill a :class:`~rates_engine.market.snapshot.MarketSnapshot`.

``file`` is the offline one every test uses. ``fred`` and ``banxico`` touch
the network, and both do so inside the call, never at import. Neither is
re-exported here: reaching for one should be a deliberate import, because
it is the moment a calculation stops being reproducible offline.
"""

from rates_engine.market.providers.file import (
    FuturesSettlement,
    load_series_csv,
    load_settlements_csv,
    load_snapshot_csv,
)

__all__ = [
    "FuturesSettlement",
    "load_series_csv",
    "load_settlements_csv",
    "load_snapshot_csv",
]
