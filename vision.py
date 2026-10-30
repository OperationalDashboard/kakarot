"""Seeing pictures: photos, screenshots and scanned PDFs, read by a local vision model (via Ollama)."""
import base64
import io
import os

import requests

from config import CFG
from tools import I, S, _cut, safe_path, tool

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
MAX_SIDE = 1400  # smaller pictures = much faster on a PC without a graphics card


def vision_model():
    return CFG.get("vision_model", "qwen2.5vl:3b")


def _jpeg_b64(img):
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def load_images(path, page=0, max_pages=3):
    """Return base64 JPEGs for an image file, or for PDF pages (page=0 -> first max_pages pages)."""
    from PIL import Image
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        pages = [page - 1] if page else range(min(len(pdf), max_pages))
        out = []
        for i in pages:
            if 0 <= i < len(pdf):
                out.append(_jpeg_b64(pdf[i].render(scale=1.6).to_pil()))
        if not out:
            raise ValueError(f"The PDF has {len(pdf)} pages.")
        return out
    if ext not in IMAGE_EXT:
        raise ValueError(f"Not a picture or PDF: {os.path.basename(path)}")
    with Image.open(path) as img:
        img.seek(0)
        return [_jpeg_b64(img)]


def ask_vision(images, question):
    body = {"model": vision_model(), "stream": False, "keep_alive": CFG.get("vision_keep_alive", "3m"),
            "messages": [{"role": "user", "content": question, "images": images}],
            "options": {"num_ctx": 8192, "temperature": 0.2}}
    r = requests.post(CFG["ollama_url"].rstrip("/") + "/api/chat", json=body, timeout=900)
    if r.status_code == 404 or "not found" in r.text.lower()[:300]:
        raise RuntimeError(f"The picture brain ({vision_model()}) isn't downloaded yet. "
                           "Run get-advanced-models.bat once.")
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
    return (r.json().get("message") or {}).get("content", "").strip()


@tool("look_at_image",
      "LOOK at a picture with your own eyes: photos, screenshots, scanned documents, receipts, tickets, charts, or a "
      "scanned PDF. Ask a specific question, e.g. 'Read all the text', 'What is the total amount?', "
      "'Describe this photo'. Use it for any attached picture and for screenshots you took.",
      {"path": S("Path of the image (.png .jpg .webp ...) or PDF."),
       "question": S("What you want to know about the picture."),
       "page": I("For PDFs: page number to look at (0 = first 3 pages).")}, ["path", "question"])
def look_at_image(path, question, page=0):
    p = safe_path(path)
    imgs = load_images(p, int(page or 0))
    answer = ask_vision(imgs, question)
    return _cut(f"What I see in {os.path.basename(p)}:\n{answer}", 8000)
