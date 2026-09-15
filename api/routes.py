"""Registers all API routers on the FastAPI app."""
from fastapi import FastAPI
from api import upload, invoices, health
from database.database import init_db

app = FastAPI(title="AI Invoice OCR API", version="1.0.0")
init_db()  # ensure lightweight sqlite migrations are applied for direct API/TestClient use

app.include_router(health.router, tags=["health"])
app.include_router(upload.router, prefix="/api/upload", tags=["upload"])
app.include_router(invoices.router, prefix="/api/invoices", tags=["invoices"])
