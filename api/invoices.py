from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from services.invoice_service import list_invoices, get_invoice, search_invoices, get_dashboard_stats
from services.export_service import export_invoices

router = APIRouter()


@router.get("/")
def get_invoices(limit: int = 100, status: str | None = None):
    return list_invoices(limit=limit, status=status)


@router.get("/stats")
def stats():
    return get_dashboard_stats()


@router.get("/search")
def search(q: str = Query(..., min_length=1)):
    return search_invoices(q)


@router.get("/{invoice_id}")
def get_one(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/export/{fmt}")
def export(fmt: str, status: str | None = None):
    invoices = list_invoices(limit=1000, status=status)
    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices to export")
    path = export_invoices(invoices, fmt=fmt)
    return FileResponse(path, filename=path.split("/")[-1])
