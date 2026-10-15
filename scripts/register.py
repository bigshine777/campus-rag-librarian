"""data/inbox 配下のPDF・画像・Excel・PowerPointをSupabaseに登録する（ローカル動作確認用）
使い方: python scripts/register.py
"""

from pathlib import Path

from db import connect
from extractors import PPTX_MIME, XLSX_MIME, pages_for
from registration import register_document

INBOX_DIR = Path(__file__).resolve().parent.parent / "data" / "inbox"

EXTENSION_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".xlsx": XLSX_MIME,
    ".pptx": PPTX_MIME,
}


def main() -> None:
    file_paths = sorted(
        p for p in INBOX_DIR.glob("*") if p.suffix.lower() in EXTENSION_MIME
    )
    if not file_paths:
        print(f"NG: {INBOX_DIR} に対応ファイルが見つかりません")
        return

    with connect() as conn:
        for file_path in file_paths:
            mime_type = EXTENSION_MIME[file_path.suffix.lower()]
            file_bytes = file_path.read_bytes()
            pages = pages_for(mime_type, file_bytes)

            chunk_count = register_document(
                conn,
                source_type="local_file",
                source_id=file_path.name,
                file_name=file_path.name,
                file_bytes=file_bytes,
                pages=pages,
            )
            if chunk_count < 0:
                print(f"SKIP: {file_path.name}（登録済み）")
            else:
                print(f"OK: {file_path.name}（{chunk_count}チャンク登録）")


if __name__ == "__main__":
    main()
