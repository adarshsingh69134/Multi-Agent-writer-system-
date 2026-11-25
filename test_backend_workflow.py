#!/usr/bin/env python

import os
from dotenv import load_dotenv
from backend import run_report_stream, ReportState

# Load environment variables
load_dotenv()

# Test topic and settings
topic = "TestTopic"
user_settings = {"report_length": "detailed", "tone": "professional"}

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

# Test the run_report_stream function
print("Testing run_report_stream...")
try:
    for event, data in run_report_stream(topic, user_settings):
        print(f"Event: {event}")
        print(f"Data: {data}")
        if event == "done":
            print("Workflow completed successfully!")
            break
    else:
        print("Workflow did not complete as expected.")
        print("Check for errors in the backend or environment setup.")
except Exception as e:
    import traceback
    print(f"Error during workflow execution: {e}")
    traceback.print_exc()