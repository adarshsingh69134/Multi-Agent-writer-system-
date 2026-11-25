#!/usr/bin/env python

import os
from dotenv import load_dotenv
from backend import run_report_stream, ReportState

# Load environment variables
load_dotenv()

# Mock checkpointer for testing
class MockCheckpointer:
    def __init__(self):
        self.states = {}

    def load_state(self, topic: str) -> dict:
        return self.states.get(topic, {})

    def save_state(self, topic: str, state: dict) -> None:
        self.states[topic] = state

# Test topic and settings
topic = "TestTopic"
user_settings = {"report_length": "detailed", "tone": "professional"}

# Create a mock checkpointer
checkpointer = MockCheckpointer()

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
    revision_round=0
)

# Test run_report_stream with the mock checkpointer
print("Testing run_report_stream...")
try:
    for event, data in run_report_stream(topic, user_settings, checkpointer):
        print(f"Event: {event}")
        print(f"Data: {data}")
        if event == "done":
            print("Workflow completed successfully!")
            break
    else:
        print("Workflow did not complete as expected.")
except Exception as e:
    import traceback
    print(f"Error during workflow execution: {e}")
    traceback.print_exc()