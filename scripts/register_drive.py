"""Google Driveのinboxフォルダにある未登録のPDF・画像・Excel・PowerPointをSupabaseに登録する
GitHub Actionsから定期実行される想定
使い方: python scripts/register_drive.py
"""

import io
import os
import time
import unicodedata

import psycopg
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from db import connect
from extractors import GOOGLE_NATIVE_EXPORT_MIME, pages_for
from registration import existing_content_hash, prune_missing_documents, register_document

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

SUPPORTED_MIME_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    *GOOGLE_NATIVE_EXPORT_MIME.keys(),
]


def build_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def with_retry(func, retries: int = 3):
    for attempt in range(retries):
        try:
            return func()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)


def list_all_folders(service) -> dict[str, dict]:
    query = "trashed = false and mimeType = 'application/vnd.google-apps.folder'"
    folders: dict[str, dict] = {}
    page_token = None
    while True:
        results = with_retry(
            lambda: service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, parents)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )
        for folder in results.get("files", []):
            parents = folder.get("parents") or []
            folders[folder["id"]] = {
                "name": folder["name"],
                "parent": parents[0] if parents else None,
            }
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return folders


def category_for_file(
    folders: dict[str, dict], root_folder_ids: set[str], file_parent_id: str | None
) -> str | None:
    current = file_parent_id
    while current and current not in root_folder_ids:
        parent = folders.get(current, {}).get("parent")
        if parent in root_folder_ids:
            name = folders.get(current, {}).get("name")
            return unicodedata.normalize("NFC", name) if name else None
        current = parent
    return None


def root_category_for_file(
    folders: dict[str, dict], root_folder_ids: set[str], file_parent_id: str | None
) -> str | None:
    current = file_parent_id
    while current:
        if current in root_folder_ids:
            name = folders.get(current, {}).get("name")
            return unicodedata.normalize("NFC", name) if name else None
        current = folders.get(current, {}).get("parent")
    return None


def collect_descendant_folder_ids(
    folders: dict[str, dict], root_folder_id: str
) -> list[str]:
    children: dict[str, list[str]] = {}
    for folder_id, info in folders.items():
        children.setdefault(info["parent"], []).append(folder_id)

    result = []
    stack = list(children.get(root_folder_id, []))
    while stack:
        folder_id = stack.pop()
        result.append(folder_id)
        stack.extend(children.get(folder_id, []))
    return result


def list_all_supported_files(service) -> list[dict]:
    mime_conditions = " or ".join(f"mimeType = '{m}'" for m in SUPPORTED_MIME_TYPES)
    mime_conditions += " or mimeType contains 'image/'"
    query = f"trashed = false and ({mime_conditions})"

    files = []
    page_token = None
    while True:
        results = with_retry(
            lambda: service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, parents)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file(service, file_id: str, mime_type: str) -> tuple[bytes, str]:
    export_mime = GOOGLE_NATIVE_EXPORT_MIME.get(mime_type)
    if export_mime:
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        effective_mime = export_mime
    else:
        request = service.files().get_media(fileId=file_id)
        effective_mime = mime_type

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = with_retry(downloader.next_chunk)
    return buffer.getvalue(), effective_mime


def main() -> None:
    root_folder_ids = [
        folder_id.strip()
        for folder_id in os.environ["GOOGLE_DRIVE_INBOX_FOLDER_IDS"].split(",")
        if folder_id.strip()
    ]
    service = build_drive_service()

    folders = list_all_folders(service)
    root_folder_id_set = set(root_folder_ids)
    all_folder_ids = []
    seen_folder_ids = set()
    for root_folder_id in root_folder_ids:
        for folder_id in [
            root_folder_id,
            *collect_descendant_folder_ids(folders, root_folder_id),
        ]:
            if folder_id not in seen_folder_ids:
                seen_folder_ids.add(folder_id)
                all_folder_ids.append(folder_id)

    folder_id_set = set(all_folder_ids)
    files = []
    seen_file_ids = set()
    for file in list_all_supported_files(service):
        parents = file.get("parents") or []
        if file["id"] in seen_file_ids or not any(p in folder_id_set for p in parents):
            continue
        seen_file_ids.add(file["id"])
        files.append(file)

    if not files:
        print(f"NG: 指定フォルダ({len(all_folder_ids)}件、サブフォルダ含む)に対応ファイルが見つかりません")
        return

    conn = connect()
    try:
        for file in files:
            try:
                md5 = file.get("md5Checksum")
                drive_hash = f"drive-md5:{md5}" if md5 else None
                if drive_hash:
                    with conn.cursor() as cur:
                        unchanged = (
                            existing_content_hash(cur, "drive_file", file["id"])
                            == drive_hash
                        )
                    if unchanged:
                        print(f"SKIP: {file['name']}（登録済み、ダウンロード省略）")
                        continue

                file_bytes, effective_mime = download_file(
                    service, file["id"], file["mimeType"]
                )
                pages = pages_for(effective_mime, file_bytes)

                parent_id = (file.get("parents") or [None])[0]
                category = category_for_file(folders, root_folder_id_set, parent_id)
                root_category = root_category_for_file(folders, root_folder_id_set, parent_id)

                chunk_count = register_document(
                    conn,
                    source_type="drive_file",
                    source_id=file["id"],
                    file_name=file["name"],
                    file_bytes=file_bytes,
                    pages=pages,
                    content_hash=drive_hash,
                    category=category,
                    root_category=root_category,
                )
            except psycopg.OperationalError as exc:
                print(f"NG: {file['name']}（DB接続エラー、再接続します: {exc!r}）")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = connect()
                continue
            except Exception as exc:
                print(f"NG: {file['name']}（処理失敗: {exc!r}）")
                continue

            if chunk_count < 0:
                print(f"SKIP: {file['name']}（登録済み）")
            else:
                print(f"OK: {file['name']}（{chunk_count}チャンク登録）")

        removed = prune_missing_documents(conn, "drive_file", seen_file_ids)
        for _source_id, file_name in removed:
            print(f"REMOVE: {file_name}（Driveから見つからなくなったため削除）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
