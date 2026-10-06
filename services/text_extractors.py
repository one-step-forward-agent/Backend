from pathlib import Path

from docx import Document
from pypdf import PdfReader


# Upper bounds for untrusted documents: page count and extracted text size
MAX_PDF_PAGES = 200
MAX_TEXT_CHARS = 200_000


def extract_pdf(path: str) -> str:
    reader = PdfReader(path)
    parts, size = [], 0
    for page in reader.pages[:MAX_PDF_PAGES]:
        text = page.extract_text() or ""
        parts.append(text)
        size += len(text)
        if size >= MAX_TEXT_CHARS:
            break
    return "\n".join(parts).strip()[:MAX_TEXT_CHARS]


def extract_docx(path: str) -> str:
    document = Document(path)
    return "\n".join(paragraph.text for paragraph in document.paragraphs).strip()[:MAX_TEXT_CHARS]


def extract_document(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise ValueError("Поддерживаются только PDF и DOCX")
    try:
        return extract_pdf(path) if suffix == ".pdf" else extract_docx(path)
    except Exception as error:  # malformed or hostile file: report it, don't crash
        raise ValueError("Не удалось прочитать документ") from error
