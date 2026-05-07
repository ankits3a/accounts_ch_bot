import io
from pathlib import Path
from PIL import Image


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}


def is_pdf(filename: str) -> bool:
    return Path(filename).suffix.lower() == ".pdf"


def image_to_pdf_bytes(image_path: str) -> bytes:
    """Convert an image file to PDF bytes using Pillow."""
    try:
        import img2pdf
        with open(image_path, "rb") as f:
            return img2pdf.convert(f)
    except Exception:
        img = Image.open(image_path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PDF")
        return buf.getvalue()
