"""ASGI application entry point."""

from fastapi import FastAPI

app = FastAPI(title="LabTrack API")


@app.get("/health/live", tags=["health"], include_in_schema=False)
async def health_live() -> dict[str, str]:
    """Report that the API process is running."""
    return {"status": "ok"}
