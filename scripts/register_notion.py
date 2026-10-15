"""Notionのバックログデータベースの各ページをSupabaseに登録する
GitHub Actionsから定期実行される想定
使い方: python scripts/register_notion.py
"""

import os

from notion_client import Client

from db import connect
from registration import prune_missing_documents, register_document

HEADING_MARKS = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}
EXCLUDED_STATUSES = ["アーカイブ", "却下"]
EXCLUDED_PAGE_IDS: set[str] = set()


def rich_text_to_plain(rich_text: list[dict]) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def fetch_all_blocks(notion: Client, block_id: str) -> list[dict]:
    blocks = []
    cursor = None
    while True:
        response = notion.blocks.children.list(block_id=block_id, start_cursor=cursor)
        blocks.extend(response["results"])
        if not response.get("has_more"):
            break
        cursor = response["next_cursor"]
    return blocks


def block_to_markdown(notion: Client, block: dict) -> list[str]:
    block_type = block["type"]
    content = block.get(block_type, {}) if isinstance(block.get(block_type), dict) else {}
    lines = []

    if block_type == "paragraph":
        lines.append(rich_text_to_plain(content.get("rich_text", [])))
    elif block_type in HEADING_MARKS:
        mark = HEADING_MARKS[block_type]
        lines.append(f"{mark} {rich_text_to_plain(content.get('rich_text', []))}")
    elif block_type == "bulleted_list_item":
        lines.append(f"- {rich_text_to_plain(content.get('rich_text', []))}")
    elif block_type == "numbered_list_item":
        lines.append(f"1. {rich_text_to_plain(content.get('rich_text', []))}")
    elif block_type == "to_do":
        checked = "x" if content.get("checked") else " "
        lines.append(f"- [{checked}] {rich_text_to_plain(content.get('rich_text', []))}")
    elif block_type in ("quote", "callout"):
        lines.append(f"> {rich_text_to_plain(content.get('rich_text', []))}")
    elif block_type == "code":
        code_text = rich_text_to_plain(content.get("rich_text", []))
        language = content.get("language", "")
        lines.append(f"```{language}\n{code_text}\n```")
    elif block_type == "divider":
        lines.append("---")
    elif block_type == "table_row":
        cells = content.get("cells", [])
        row_text = " | ".join(rich_text_to_plain(cell) for cell in cells)
        lines.append(f"| {row_text} |")
    elif "rich_text" in content:
        lines.append(rich_text_to_plain(content.get("rich_text", [])))

    if block.get("has_children") and block_type not in ("child_page", "child_database"):
        for child in fetch_all_blocks(notion, block["id"]):
            lines.extend(block_to_markdown(notion, child))

    return lines


def page_to_markdown(notion: Client, page_id: str) -> str:
    blocks = fetch_all_blocks(notion, page_id)
    lines: list[str] = []
    for block in blocks:
        lines.extend(block_to_markdown(notion, block))
    return "\n\n".join(line for line in lines if line.strip())


def list_data_source_pages(notion: Client, data_source_id: str) -> list[dict]:
    query_filter = {
        "and": [
            {"property": "ステータス", "select": {"does_not_equal": status}}
            for status in EXCLUDED_STATUSES
        ]
    }

    pages = []
    cursor = None
    while True:
        response = notion.data_sources.query(
            data_source_id=data_source_id, start_cursor=cursor, filter=query_filter
        )
        pages.extend(response["results"])
        if not response.get("has_more"):
            break
        cursor = response["next_cursor"]
    return pages


def register_page(notion: Client, conn, page: dict) -> None:
    page_id = page["id"]
    title_prop = page["properties"].get("タイトル", {}).get("title", [])
    title = rich_text_to_plain(title_prop) or "(無題)"

    markdown_text = page_to_markdown(notion, page_id)
    if not markdown_text.strip():
        print(f"SKIP: {title}（本文なし）")
        return

    chunk_count = register_document(
        conn,
        source_type="notion",
        source_id=page_id,
        file_name=title,
        file_bytes=markdown_text.encode("utf-8"),
        pages=[("markdown", markdown_text)],
        category="Notion",
    )
    if chunk_count < 0:
        print(f"SKIP: {title}（登録済み）")
    else:
        print(f"OK: {title}（{chunk_count}チャンク登録）")


def main() -> None:
    notion = Client(auth=os.environ["NOTION_API_KEY"])
    data_source_id = os.environ["NOTION_DATA_SOURCE_ID"]

    pages = [
        page
        for page in list_data_source_pages(notion, data_source_id)
        if page["id"] not in EXCLUDED_PAGE_IDS
    ]
    if not pages:
        print(f"NG: データソース {data_source_id} にページが見つかりません")
        return

    with connect() as conn:
        for page in pages:
            register_page(notion, conn, page)

        current_ids = {page["id"] for page in pages}
        removed = prune_missing_documents(conn, "notion", current_ids)
        for _source_id, title in removed:
            print(f"REMOVE: {title}（対象外になったため削除）")


if __name__ == "__main__":
    main()
