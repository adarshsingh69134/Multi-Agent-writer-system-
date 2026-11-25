#!/usr/bin/env python

import os
import json
from dotenv import load_dotenv
from backend import workflow, ReportState

# Load environment variables
load_dotenv()

# Test topic and settings
topic = "TestTopic"
user_settings = {"report_length": "detailed", "tone": "professional"}

# Mock checkpointer for testing
class MockCheckpointer:
    def __init__(self):
        self.states = {}

    def save_state(self, topic: str, state: dict) -> None:
        self.states[topic] = state

    def load_state(self, topic: str) -> dict:
        return self.states.get(topic, {})

# Create a minimal state for testing
state = ReportState(
    topic=topic,
    user_settings=user_settings,
    plan={},
    research_notes=[],
    literature_summaries=[],
    draft="",
    feedback={},
    citations={},
    revision_round=0,
    message="Test message for the planner node"
)

# Initialize mock checkpointer
checkpointer = MockCheckpointer()

# Compile the workflow
# workflow is already compiled in backend.py, so we don't need to compile it again

# Test the workflow manually
print("Testing workflow manually...")
try:
    # Save initial state
    checkpointer.save_state(topic, state)

    # Run the workflow manually
    for update in workflow.stream(state):
        print(f"Event: {update}")

    # Save the final state
    final_state = state
    checkpointer.save_state(topic, final_state)
    print("Workflow completed successfully!")
except Exception as e:
    import traceback
    print(f"Error during workflow execution: {e}")
    traceback.print_exc()