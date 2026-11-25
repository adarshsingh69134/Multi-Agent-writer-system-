import os
from typing import Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr
from tavily import TavilyClient


class TavilySearchInput(BaseModel):
    query: str = Field(..., description="The search query to look up on the web.")


class TavilySearchTool(BaseTool):
    name: str = "tavily_search"
    description: str = (
        "Search the web using the Tavily API for current facts, data, and source URLs."
    )
    args_schema: Type[BaseModel] = TavilySearchInput

    _tavily_client: Optional[TavilyClient] = PrivateAttr(default=None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        api_key = os.environ.get("TAVILY_API_KEY")
        self._tavily_client = TavilyClient(api_key=api_key) if api_key else None

    def _run(self, query: str) -> str:
        """Executes a search query using the Tavily API and returns a compact result."""
        if not self._tavily_client:
            api_key = os.environ.get("TAVILY_API_KEY")
            if not api_key:
                return "Error: TAVILY_API_KEY environment variable is not set."
            self._tavily_client = TavilyClient(api_key=api_key)

        try:
            response = self._tavily_client.search(
                query=query,
                search_depth="basic",
                include_answer=True,
                max_results=3,
            )

            parts = []
            if response.get("answer"):
                parts.append(f"Summary: {response['answer'][:500]}\n")

            parts.append("Results:")
            total_len = sum(len(p) for p in parts)

            for item in response.get("results", []):
                snippet = (item.get("content") or "").strip()[:300]
                block = (
                    f"- {item.get('title', 'Untitled')}\n"
                    f"  {snippet}\n"
                    f"  URL: {item.get('url', '')}\n"
                )
                if total_len + len(block) > 1800:
                    break
                parts.append(block)
                total_len += len(block)

            return "\n".join(parts)

        except Exception as e:
            return f"Error executing search query '{query}': {e}"