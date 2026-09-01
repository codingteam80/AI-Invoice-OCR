from fastapi import APIRouter, UploadFile, File, HTTPException
from services.upload_service import handle_upload, UploadError
from services.invoice_service import process_invoice_file

router = APIRouter()


@router.post("/")
async def upload_invoice(file: UploadFile = File(...)):
    file_bytes = await file.read()
    try:
        saved_path = handle_upload(file_bytes, file.filename)
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    results = process_invoice_file(saved_path, original_filename=file.filename)
    if not any(r.get("success") for r in results):
        # Every invoice found on this file failed or was a duplicate —
        # surface the first error rather than a bare empty list.
        raise HTTPException(status_code=422, detail=results[0].get("error"))
    return {"invoices": results}
