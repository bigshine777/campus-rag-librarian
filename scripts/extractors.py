"""各ファイル形式を「ページのリスト」に変換する
ページは ("image", 画像bytes) または ("markdown", 文字列) のタプル
画像はgpt-5.4-miniに読ませ、Excel/PowerPointは構造化データなので
直接Markdownに変換する（画像化してAIに読ませるより安く確実）
"""

import io

import pymupdf

PAGE_IMAGE_DPI = 150

GOOGLE_NATIVE_EXPORT_MIME = {
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def pdf_pages(pdf_bytes: bytes) -> list[tuple[str, tuple[bytes, str | None]]]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        image_bytes = page.get_pixmap(dpi=PAGE_IMAGE_DPI).tobytes("png")
        page_text = page.get_text().strip() or None
        pages.append(("image", (image_bytes, page_text)))
    return pages


def image_pages(image_bytes: bytes) -> list[tuple[str, tuple[bytes, str | None]]]:
    return [("image", (image_bytes, None))]


def xlsx_pages(xlsx_bytes: bytes) -> list[tuple[str, str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    pages = []
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        rows = [row for row in rows if any(cell is not None for cell in row)]
        if not rows:
            continue

        def format_row(row) -> str:
            cells = ["" if cell is None else str(cell) for cell in row]
            return "| " + " | ".join(cells) + " |"

        lines = [f"## {sheet.title}", "", format_row(rows[0])]
        lines.append("|" + "---|" * len(rows[0]))
        for row in rows[1:]:
            lines.append(format_row(row))
        pages.append(("markdown", "\n".join(lines)))
    return pages


def pptx_pages(pptx_bytes: bytes) -> list[tuple[str, str]]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(pptx_bytes))
    pages = []
    for slide in presentation.slides:
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    lines.append("| " + " | ".join(cells) + " |")
        pages.append(("markdown", "\n\n".join(lines)))
    return pages


def pages_for(mime_type: str, file_bytes: bytes) -> list[tuple[str, bytes | str]]:
    if mime_type == "application/pdf":
        return pdf_pages(file_bytes)
    if mime_type.startswith("image/"):
        return image_pages(file_bytes)
    if mime_type == XLSX_MIME:
        return xlsx_pages(file_bytes)
    if mime_type == PPTX_MIME:
        return pptx_pages(file_bytes)
    raise ValueError(f"未対応のmimeTypeです: {mime_type}")
