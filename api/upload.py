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

    result = process_invoice_file(saved_path)
    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error"))
    return result
