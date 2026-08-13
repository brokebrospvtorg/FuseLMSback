"""
Local-disk storage for fee proof uploads.

Deliberately not a generic "file storage" abstraction — this is scoped to
fee proofs specifically, per the architecture decision to avoid external
cloud storage for now. If a second upload type (e.g. profile photos) is
added later, treat this as the template rather than making it generic
prematurely.
"""
import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings


def _ensure_upload_dir() -> Path:
    upload_dir = Path(settings.FEE_PROOF_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def save_fee_proof_file(upload: UploadFile) -> str:
    """
    Validates and saves an uploaded fee proof to local disk.

    Returns the relative file path to store in fee_proofs.file_url.
    Raises HTTPException(400) if the file fails validation.

    Security notes:
    - Filename is always a fresh uuid4 + the original extension — the
      client-supplied filename is never used to build a path, so there's
      no path-traversal risk from something like "../../etc/passwd.jpg".
    - Extension is checked against an allowlist, not a blocklist.
    - Size is enforced server-side by reading in chunks and bailing out
      the moment the cap is exceeded, rather than trusting Content-Length
      (which a client can lie about) or the frontend's compression step.
    """
    original_name = upload.filename or ""
    ext = Path(original_name).suffix.lower()
    if ext not in settings.FEE_PROOF_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(settings.FEE_PROOF_ALLOWED_EXTENSIONS)}",
        )

    upload_dir = _ensure_upload_dir()
    stored_name = f"{uuid.uuid4()}{ext}"
    dest_path = upload_dir / stored_name

    max_bytes = int(settings.FEE_PROOF_MAX_SIZE_MB * 1024 * 1024)
    written = 0
    chunk_size = 1024 * 1024  # 1MB

    try:
        with open(dest_path, "wb") as out_file:
            while True:
                chunk = upload.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out_file.close()
                    os.remove(dest_path)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"File exceeds the {settings.FEE_PROOF_MAX_SIZE_MB}MB limit.",
                    )
                out_file.write(chunk)
    finally:
        upload.file.close()

    if written == 0:
        os.remove(dest_path)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    # Stored as a path relative to FEE_PROOF_UPLOAD_DIR, not an absolute
    # filesystem path — keeps file_url portable if the app ever moves
    # working directories (e.g. local -> Render disk) without a data fix.
    return stored_name


def resolve_fee_proof_path(stored_name: str) -> Path:
    """Turns a stored relative name back into a real path for reading."""
    return Path(settings.FEE_PROOF_UPLOAD_DIR) / stored_name


def delete_fee_proof_file(stored_name: str) -> bool:
    """Deletes the physical file if it exists. Returns True if it was removed."""
    path = resolve_fee_proof_path(stored_name)
    if path.exists() and path.is_file():
        os.remove(path)
        return True
    return False
