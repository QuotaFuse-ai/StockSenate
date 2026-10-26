"""DataAccessLayer: providers, routing and the DataEnvelope contract."""

from stock_senate.data.envelope import DataEnvelope
from stock_senate.data.router import DataAccessLayer, summarize

__all__ = ["DataAccessLayer", "DataEnvelope", "summarize"]
