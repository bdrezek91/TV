"""Local, event-driven Paper Trading Engine. No live order placement code
exists anywhere in this package — every fill is simulated deterministically
from OHLCV/order-book inputs the caller supplies."""
