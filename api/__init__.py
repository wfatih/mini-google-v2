"""
api — FastAPI REST server for mini-google-v2.

Public API:
    create_app  — Factory that returns the configured FastAPI application.
    run_server  — Start the uvicorn ASGI server (blocking call).
"""

from api.server import create_app, run_server

__all__ = ["create_app", "run_server"]
