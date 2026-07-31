"""Registers all API routers on the FastAPI app."""
from fastapi import FastAPI
from api import upload, invoices, health

app = FastAPI(title="AI Invoice OCR API", version="1.0.0")

app.include_router(health.router, tags=["health"])
app.include_router(upload.router, prefix="/api/upload", tags=["upload"])
app.include_router(invoices.router, prefix="/api/invoices", tags=["invoices"])
