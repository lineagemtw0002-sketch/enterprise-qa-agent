"""联邦查询层（§3.5）。对外只暴露引擎、缓存和这两个协议/数据结构。"""

from src.ops.federation.cache import FederatedQueryCache
from src.ops.federation.engine import (
    DEFAULT_TIMEOUT_SECONDS,
    ConnectionDirectory,
    ConnectionRef,
    FederatedQueryEngine,
    describe_unavailable,
)

__all__ = [
    "FederatedQueryCache",
    "FederatedQueryEngine",
    "ConnectionDirectory",
    "ConnectionRef",
    "describe_unavailable",
    "DEFAULT_TIMEOUT_SECONDS",
]
