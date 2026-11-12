"""
core/doc_importer.py — Importatore Automatico di Slide, PDF e Immagini per FlowLocal
Caratteristiche:
- Estrazione testo digitale pagina per pagina con PyMuPDF (fitz)
- Fallback automatico su RapidOCR (ONNX Runtime) per scansioni o pagine con < 50 caratteri digitali
- Supporto per immagini (.png, .jpg, .jpeg, .webp, .bmp) con OCR completo
- Salvataggio strutturato in SQLite (study_notes) con note_type='slide'
- Supporto gerarchico: profile, subject, topic
- Feedback di avanzamento tramite progress_callback opzionale
"""

import os
import io
import time
from pathlib import Path
from typing import Optional, Callable, Dict, Any

import numpy as np
from PIL import Image

# Import condizionali / protetti
try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine: Optional[RapidOCR] = None

    def get_ocr_engine() -> RapidOCR:
        global _ocr_engine
        if _ocr_engine is None:
            _ocr_engine = RapidOCR()
        return _ocr_engine
except ImportError:
    get_ocr_engine = lambda: None


def _extract_ocr_from_image(image_input) -> str:
    """Esegue OCR su un'immagine (PIL Image, ndarray o path) e restituisce il testo unito."""
    engine = get_ocr_engine()
    if engine is None:
        return ""
    try:
        if isinstance(image_input, Image.Image):
            arr = np.array(image_input)
        elif isinstance(image_input, (str, Path)):
            arr = np.array(Image.open(str(image_input)))
        else:
            arr = image_input
        res, _ = engine(arr)
        if not res:
            return ""
        lines = [line[1].strip() for line in res if len(line) > 1 and line[1] and line[1].strip()]
        return "\n".join(lines)
    except Exception as e:
        print(f"[DocImporter OCR Error] {e}")
        return ""


def import_document(
    file_path: str,
    profile: str = "Alessandro",
    subject: str = "Generale",
    topic: str = "Generale",
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, Any]:
    """
    Importa un file PDF o immagine, estrae testo nativo o tramite OCR,
    e crea una nuova voce 'slide' nel database locale Notekeeper.
    
    Args:
        file_path: Percorso del file sul filesystem (.pdf, .png, .jpg, .jpeg, .webp, .bmp).
        profile: Profilo studente ('Alessandro' o 'Giada').
        subject: Materia di appartenenza (es. 'Meccanica dei Robot').
        topic: Argomento specifico dentro la materia (es. 'Cinematica').
        progress_callback: Callback opzionale (current_page, total_pages, status_message).
        
    Returns:
        Dict con i dettagli della nota creata: id, title, pages, ocr_pages, file_path.
    """
    from database.db_manager import db

    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"File non trovato: {path}")

    ext = path.suffix.lower()
    doc_title = path.stem.replace("_", " ").strip()
    if not doc_title:
        doc_title = "Documento Importato"

    pages_text = []
    total_pages = 1
    ocr_pages_count = 0

    # ===== CASO 1: FILE PDF =====
    if ext == ".pdf":
        if fitz is None:
            raise RuntimeError("Libreria PyMuPDF (fitz) non installata. Esegui 'uv add pymupdf'.")

        doc = fitz.open(str(path))
        total_pages = len(doc)

        for page_idx in range(total_pages):
            page_num = page_idx + 1
            if progress_callback:
                progress_callback(page_num, total_pages, f"Elaborazione pagina {page_num}/{total_pages}...")

            page = doc[page_idx]
            native_text = (page.get_text("text") or "").strip()

            # Se la pagina ha meno di 50 caratteri nativi (es. scansione, slide grafica o appunti scritti a mano)
            if len(native_text) < 50:
                try:
                    pix = page.get_pixmap(dpi=150)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    ocr_text = _extract_ocr_from_image(img).strip()
                    if len(ocr_text) > len(native_text):
                        native_text = ocr_text
                        ocr_pages_count += 1
                except Exception as ocr_err:
                    print(f"[DocImporter] Avviso OCR su pagina {page_num}: {ocr_err}")

            page_header = f"### 📄 Pagina {page_num}\n"
            content = native_text if native_text else "*(Nessun testo rilevato in questa pagina)*"
            pages_text.append(f"{page_header}\n{content}\n")

        doc.close()

    # ===== CASO 2: IMMAGINI (.png, .jpg, .jpeg, .webp, .bmp) =====
    elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        if progress_callback:
            progress_callback(1, 1, "Esecuzione OCR sull'immagine...")
        ocr_text = _extract_ocr_from_image(path).strip()
        ocr_pages_count = 1
        content = ocr_text if ocr_text else "*(Nessun testo rilevato tramite OCR)*"
        pages_text.append(f"### 🖼️ Immagine Slide\n\n{content}\n")

    # ===== CASO 3: FILE TESTUALI (.txt, .md) =====
    elif ext in (".txt", ".md"):
        if progress_callback:
            progress_callback(1, 1, "Lettura testo...")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
        except Exception:
            with open(path, "r", encoding="latin-1", errors="replace") as f:
                content = f.read().strip()
        pages_text.append(content)

    else:
        raise ValueError(f"Formato non supportato: {ext}. Supportati: .pdf, .png, .jpg, .jpeg, .webp, .bmp, .txt, .md")

    # Formattazione Markdown finale arricchita per Notekeeper
    full_markdown_parts = [
        f"# 📑 Slide: {doc_title}",
        f"- **File di origine:** `{path.name}`",
        f"- **Pagine elaborate:** {total_pages} (di cui {ocr_pages_count} tramite OCR visivo)",
        f"- **Data di importazione:** {time.strftime('%Y-%m-%d %H:%M')}",
        f"- **Materia:** `{subject}` | **Argomento:** `{topic}`",
        f"- **Profilo:** `{profile}`",
        "\n---\n",
        "\n\n".join(pages_text)
    ]
    final_content = "\n".join(full_markdown_parts)

    # Salvataggio nel Database Notekeeper
    note_id = db.add_study_note(
        title=f"Slide: {doc_title}",
        content_markdown=final_content,
        note_type="slide",
        tags="slide,pdf,ocr" if ocr_pages_count > 0 else "slide,pdf",
        profile=profile,
        subject=subject,
        topic=topic
    )

    if progress_callback:
        progress_callback(total_pages, total_pages, "Importazione completata con successo!")

    return {
        "id": note_id,
        "title": f"Slide: {doc_title}",
        "pages": total_pages,
        "ocr_pages": ocr_pages_count,
        "file_path": str(path),
        "profile": profile,
        "subject": subject,
        "topic": topic
    }

