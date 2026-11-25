"""
LangGraph workflow for the four agents:
  Researcher -> Writer -> Reviewer -> (revise -> Writer) or (done -> Final Editor) -> END

The node functions (each runs its CrewAI agent) live in backend.py, so this
file only builds the graph. Put it next to backend.py.
"""
import os

from langgraph.graph import StateGraph, END

from backend import (
    ReportState,
    researcher_node,
    writer_node,
    reviewer_node,
    final_node,
)

# Maximum number of Writer drafts (the first draft plus revisions)
MAX_REVISION_ROUNDS = int(os.environ.get("AGENT_MAX_RETRIES", 3))


# -----------------------------------------------------------------------------
# Conditional logic
# -----------------------------------------------------------------------------
def should_revise(state: ReportState) -> bool:
    """True if the Reviewer did not pass the draft and rounds remain."""
    if state.get("error"):
        return False
    review_text = str(state.get("feedback", {}).get("overall_score", "")).lower()
    passed = "verdict: pass" in review_text
    return bool(review_text) and not passed and state.get("revision_round", 0) < MAX_REVISION_ROUNDS


# -----------------------------------------------------------------------------
# Graph construction
# -----------------------------------------------------------------------------
graph = StateGraph(ReportState)

# 1. Nodes (one per agent)
graph.add_node("researcher_node", researcher_node)
graph.add_node("writer_node", writer_node)
graph.add_node("reviewer_node", reviewer_node)
graph.add_node("final_node", final_node)

# 2. Research -> Write -> Review
graph.set_entry_point("researcher_node")
graph.add_edge("researcher_node", "writer_node")
graph.add_edge("writer_node", "reviewer_node")

# 3. Revision loop: back to the Writer, or on to the Final Editor
graph.add_conditional_edges(
    "reviewer_node",
    should_revise,
    {True: "writer_node", False: "final_node"},
)

# 4. Final Editor combines everything, then the workflow ends
graph.add_edge("final_node", END)

# Compile (keep the result: this is the runnable workflow)
workflow = graph.compile()