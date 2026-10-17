"""Controlvar Python SDK.

Usage::

    from controlvar import ExperimentationClient

    client = ExperimentationClient("http://localhost:8000", api_key="...")
    variant = client.get_variant("checkout_flow", user_id="user-123")
    if client.is_feature_enabled("new_search", "user-123"):
        ...
    client.track("user-123", "purchase", event_value=49.99, experiment_key="checkout_flow")
"""

from .client import BATCH_LIMIT, ExperimentationClient
from .hashing import consistent_hash, md5_hex
from .local import LocalEvaluationStatus, ReadyResult
from .transport import Response, Transport, TransportError, UrllibTransport
from .types import Assignment, BatchResult, ExperimentationError, FlagEvaluation
from .version import __version__

__all__ = [
    "BATCH_LIMIT",
    "Assignment",
    "BatchResult",
    "ExperimentationClient",
    "ExperimentationError",
    "FlagEvaluation",
    "LocalEvaluationStatus",
    "ReadyResult",
    "Response",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "__version__",
    "consistent_hash",
    "md5_hex",
]
