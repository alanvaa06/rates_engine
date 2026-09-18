"""Providers that fill a :class:`~rates_engine.market.snapshot.MarketSnapshot`.

``file`` is the offline one every test uses; ``fred`` is the only one that
touches the network, and it does so inside the call, never at import.
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
