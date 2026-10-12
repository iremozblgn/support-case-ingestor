from __future__ import annotations

from support_portal.fetch_strategies.base import BaseGraphQLStrategy
from support_portal.fetch_strategies.graphql_full import GraphQLFullStrategy
from support_portal.fetch_strategies.graphql_minimal import GraphQLMinimalStrategy
from support_portal.fetch_strategies.graphql_safe import GraphQLSafeStrategy

STRATEGY_REGISTRY: dict[str, type[BaseGraphQLStrategy]] = {
    "full": GraphQLFullStrategy,
    "safe": GraphQLSafeStrategy,
    "minimal": GraphQLMinimalStrategy,
}

__all__ = [
    "BaseGraphQLStrategy",
    "GraphQLFullStrategy",
    "GraphQLSafeStrategy",
    "GraphQLMinimalStrategy",
    "STRATEGY_REGISTRY",
]
