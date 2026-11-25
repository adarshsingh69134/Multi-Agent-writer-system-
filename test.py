import os
from tavily import TavilyClient

def test_tavily_tool():
    print("Testing Tavily API Direct Connection...")

    # Check if API key is set
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("Error: TAVILY_API_KEY not found in environment variables.")
        return

    try:
        # Initialize the client directly with just the API key
        client = TavilyClient(api_key=api_key)

        query = "What is the latest update on AI agents in 2026?"
        print(f"Running search query: '{query}'...")

        # Execute search
        response = client.search(
            query=query,
            search_depth="basic",  # Use basic for faster testing
            include_answer=True,
            max_results=3
        )

        # Construct readable output
        results = []
        if "answer" in response and response["answer"]:
            results.append(f"Summary: {response['answer']}\n")

        results.append("Top Results:")
        for i, result in enumerate(response.get("results", []), 1):
            results.append(f"{i}. {result.get('title')}")
            results.append(f"   URL: {result.get('url')}")
            results.append(f"   Snippet: {result.get('content')[:100]}...\n")

        output = "\n".join(results)

        print("\n--- Search Results ---")
        print(output)
        print("----------------------")

        if "Error" in output or "error" in output.lower():
            print("\nTest found errors in results.")
        else:
            print("\nTest passed successfully! API is working.")

    except TypeError as e:
        print(f"\nTypeError (likely env var issue): {e}")
        print("\nThis is likely because the environment variable format is incorrect.")
        print("Make sure TAVILY_API_KEY is set properly in your .env file.")

    except Exception as e:
        print(f"\nAn error occurred: {type(e).__name__}: {e}")

if __name__ == "__main__":
    test_tavily_tool()