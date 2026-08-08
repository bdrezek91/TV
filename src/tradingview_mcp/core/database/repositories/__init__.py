"""Repository layer: the only place that issues writes/reads against the
trading-system tables. Collector/poller/worker code calls these functions
rather than building SQLAlchemy statements inline, so persistence rules
(idempotency, "don't overwrite newer with older", UTC normalisation) live
in one tested place.
"""
