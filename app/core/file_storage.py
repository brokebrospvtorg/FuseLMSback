"""
Local-disk storage for fee proof uploads.

Deliberately not a generic "file storage" abstraction — this is scoped to
fee proofs specifically, per the architecture decision to avoid external
cloud storage for now. If a second upload type (e.g. profile photos) is
added later, treat this as the template rather than making it generic
prematurely.
"""
import io
import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.core.config import settings

# Admin Sub-Sprint 4: "The system should compress the pdf and bring it to
# the required format" (and images — the frontend's own compression pass
# is a courtesy, not something the backend trusts; a client can skip it
# entirely). Two new deps: Pillow (images) and pypdf (PDFs), both
# pure-Python/PyPI-installable, no native/system dependencies.
_IMAGE_MAX_DIMENSION = 1600  # px, longest side
_IMAGE_JPEG_QUALITY = 78
_COMPRESS_ABOVE_BYTES = 300 * 1024  # don't bother compressing small files


def _compress_image(raw: bytes) -> tuple[bytes, str]:
    """Downscales + re-encodes as JPEG if the image is large. Returns
    (bytes, new_extension). Falls back to the original bytes untouched if
    Pillow can't decode it (corrupt/unsupported) — validation elsewhere
    already gated the extension, so this is a belt-and-suspenders catch,
    not the primary defense."""
    if len(raw) <= _COMPRESS_ABOVE_BYTES:
        return raw, ""
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")  # drops alpha/CMYK edge cases JPEG can't hold
        img.thumbnail((_IMAGE_MAX_DIMENSION, _IMAGE_MAX_DIMENSION))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
        compressed = out.getvalue()
        # Only use the compressed version if it's actually smaller —
        # a tiny/already-optimized image can grow slightly under re-encode.
        return (compressed, ".jpg") if len(compressed) < len(raw) else (raw, "")
    except Exception:
        return raw, ""


def _compress_pdf(raw: bytes) -> bytes:
    """Recompresses each page's content streams. Modest (pypdf doesn't
    re-encode embedded images the way a full Ghostscript pass would), but
    real, dependency-light compression rather than a no-op — bigger tools
    like Ghostscript/PyMuPDF need native binaries this environment doesn't
    guarantee. Falls back to the original bytes on any parse failure."""
    try:
        reader = PdfReader(io.BytesIO(raw))
        writer = PdfWriter()
        for page in reader.pages:
            page.compress_content_streams()
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        compressed = out.getvalue()
        return compressed if len(compressed) < len(raw) else raw
    except Exception:
        return raw


def _ensure_upload_dir() -> Path:
    upload_dir = Path(settings.FEE_PROOF_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def save_fee_proof_file(upload: UploadFile) -> str:
    """
    Validates, compresses, and saves an uploaded fee proof to local disk.

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
      The cap is checked against the ORIGINAL upload before compression —
      compression happens after the size gate, not instead of it, so a
      malicious oversized upload is still rejected up front rather than
      accepted and then shrunk.
    """
    original_name = upload.filename or ""
    ext = Path(original_name).suffix.lower()
    if ext not in settings.FEE_PROOF_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(settings.FEE_PROOF_ALLOWED_EXTENSIONS)}",
        )

    max_bytes = int(settings.FEE_PROOF_MAX_SIZE_MB * 1024 * 1024)
    chunk_size = 1024 * 1024  # 1MB
    buffer = io.BytesIO()
    written = 0

    try:
        while True:
            chunk = upload.file.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File exceeds the {settings.FEE_PROOF_MAX_SIZE_MB}MB limit.",
                )
            buffer.write(chunk)
    finally:
        upload.file.close()

    if written == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    raw = buffer.getvalue()
    if ext == ".pdf":
        raw = _compress_pdf(raw)
    else:
        raw, new_ext = _compress_image(raw)
        if new_ext:
            ext = new_ext

    upload_dir = _ensure_upload_dir()
    stored_name = f"{uuid.uuid4()}{ext}"
    dest_path = upload_dir / stored_name
    dest_path.write_bytes(raw)

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
