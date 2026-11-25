from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from backend import run_report_stream
import json

app = FastAPI()

@app.get("/generate-report/")
async def generate_report(topic: str):
    """
    Endpoint to trigger report generation and stream progress updates.
    """
    def event_generator():
        # Iterate over your existing backend generator
        for event, data in run_report_stream(topic):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)