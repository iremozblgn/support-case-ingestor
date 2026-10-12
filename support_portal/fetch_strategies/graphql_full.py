from __future__ import annotations

from support_portal.fetch_strategies.base import BaseGraphQLStrategy


class GraphQLFullStrategy(BaseGraphQLStrategy):
    key = "full"
    name = "GraphQLFullStrategy"
    query_filename = "case_full.graphql"
    use_browser_payload = True
