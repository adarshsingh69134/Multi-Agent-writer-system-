import os
from crewai.tools import BaseTool
from tavily import TavilyClient
from pydantic import Field, PrivateAttr

class TavilySearchTool(BaseTool):
    name: str = "Tavily Search Tool"
    description: str = (
        "A search tool that uses the Tavily API to search the web for information. "
        "It is useful for answering questions that require current information, "
        "or research that requires fetching data from the web."
    )

    # Use PrivateAttr so Pydantic doesn't validate it as a public field
    _tavily_client: TavilyClient = PrivateAttr(default=None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        api_key = os.environ.get("TAVILY_API_KEY")
        if api_key:
            self._tavily_client = TavilyClient(api_key=api_key)
        else:
            self._tavily_client = None

    def _run(self, query: str) -> str:
        """
        Executes a search query using the Tavily API and returns a compact result.
        """
        if not self._tavily_client:
            return "Error: Tavily client not initialized. Please check your TAVILY_API_KEY environment variable."

        try:
            response = self._tavily_client.search(
                query=query,
                search_depth="basic",     # was "advanced": shorter snippets, fewer credits
                include_answer=True,
                max_results=3             # was 5
            )

            results = []
            if response.get("answer"):
                results.append(f"Summary: {response['answer'][:500]}\n")

            results.append("Results:")
            for result in response.get("results", []):
                snippet = (result.get("content") or "")[:300]   # cap each snippet
                results.append(f"- {result.get('title')}")
                results.append(f"  {snippet}")
                results.append(f"  URL: {result.get('url')}\n")

            return "\n".join(results)[:1800]   # hard cap on total output

        except Exception as e:
            return f"Error executing search query '{query}': {str(e)}"