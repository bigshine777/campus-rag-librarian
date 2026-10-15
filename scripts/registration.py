"""ファイルをページ単位（画像またはMarkdown文字列）に分解し、
画像ページはgpt-5.4-miniでMarkdown化した上でチャンク分割し、
Azureで埋め込んでSupabaseに登録する共通処理
register.py（ローカル）とregister_drive.py（Google Drive）から共有される
形式ごとのページ分解はextractors.pyが担当する
"""

import base64
import hashlib
import os
import re

import tiktoken
from openai import AzureOpenAI

CHUNK_TOKENS = 500
PAGE_MAX_OUTPUT_TOKENS = 8000
ENCODING = tiktoken.get_encoding("cl100k_base")

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
)
EMBEDDING_DEPLOYMENT = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
EMBEDDING_DIMENSIONS = int(os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"])
CHAT_DEPLOYMENT = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]


def is_table_separator_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    return set(stripped) <= set("|-: ")


def split_table_rows_by_tokens(header: str, body_lines: list[str], budget: int) -> list[str]:
    pieces = []
    current_rows: list[str] = []
    current_tokens = 0

    def flush() -> None:
        if current_rows:
            pieces.append(header + "\n".join(current_rows))

    for line in body_lines:
        line_tokens = len(ENCODING.encode(line + "\n"))

        if line_tokens > budget:
            flush()
            current_rows.clear()
            current_tokens = 0
            line_token_ids = ENCODING.encode(line)
            for i in range(0, len(line_token_ids), budget):
                pieces.append(header + ENCODING.decode(line_token_ids[i : i + budget]))
            continue

        if current_rows and current_tokens + line_tokens > budget:
            flush()
            current_rows.clear()
            current_tokens = 0

        current_rows.append(line)
        current_tokens += line_tokens

    flush()
    return pieces


def split_oversized_paragraph(paragraph: str, max_tokens: int) -> list[str]:
    lines = paragraph.split("\n")
    if len(lines) >= 2 and lines[0].strip().startswith("|") and is_table_separator_line(lines[1]):
        header = lines[0] + "\n" + lines[1] + "\n"
        header_tokens = len(ENCODING.encode(header))
        budget = max(max_tokens - header_tokens, 1)
        return split_table_rows_by_tokens(header, lines[2:], budget)

    body_tokens = ENCODING.encode(paragraph)
    return [
        ENCODING.decode(body_tokens[i : i + max_tokens])
        for i in range(0, len(body_tokens), max_tokens)
    ]


def chunk_text(text: str, max_tokens: int = CHUNK_TOKENS) -> list[str]:
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks = []
    current: list[str] = []
    current_tokens = 0

    def flush() -> None:
        if current:
            chunks.append("\n\n".join(current))
            current.clear()

    for paragraph in paragraphs:
        paragraph_tokens = len(ENCODING.encode(paragraph))

        if paragraph_tokens > max_tokens:
            flush()
            current_tokens = 0
            chunks.extend(split_oversized_paragraph(paragraph, max_tokens))
            continue

        if current and current_tokens + paragraph_tokens > max_tokens:
            flush()
            current_tokens = 0

        current.append(paragraph)
        current_tokens += paragraph_tokens

    flush()
    return chunks


def embed(text: str) -> list[float]:
    response = client.embeddings.create(
        model=EMBEDDING_DEPLOYMENT,
        input=text,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


def page_to_markdown(image_bytes: bytes, reference_text: str | None = None) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    instruction = (
        "このページ画像の内容をMarkdownに変換してください。"
        "表がある場合はMarkdownの表として再現してください。"
    )
    if reference_text:
        instruction += (
            "\n\nこのページから直接抜き出したテキストも参考として付けます。"
            "画像だけでは氏名などの漢字を誤読しやすいため、"
            "文字そのものはこの参考テキストを優先して使い、"
            "レイアウトや表の行・列の対応は画像を見て判断してください。\n\n"
            f"---参考テキスト---\n{reference_text}"
        )
    response = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        max_completion_tokens=PAGE_MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": instruction,
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
    )
    return response.choices[0].message.content


def existing_content_hash(cur, source_type: str, source_id: str) -> str | None:
    cur.execute(
        "select content_hash from documents where source_type = %s and source_id = %s",
        (source_type, source_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def prune_missing_documents(
    conn, source_type: str, current_source_ids: set[str]
) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "select source_id, file_name from documents where source_type = %s",
            (source_type,),
        )
        existing = cur.fetchall()

    removed = [
        (source_id, file_name)
        for source_id, file_name in existing
        if source_id not in current_source_ids
    ]

    with conn.cursor() as cur:
        for source_id, _file_name in removed:
            cur.execute(
                "delete from documents where source_type = %s and source_id = %s",
                (source_type, source_id),
            )
    conn.commit()
    return removed


def register_document(
    conn,
    source_type: str,
    source_id: str,
    file_name: str,
    file_bytes: bytes,
    pages: list[tuple[str, bytes | str]],
    content_hash: str | None = None,
    category: str | None = None,
    root_category: str | None = None,
) -> int:
    if content_hash is None:
        content_hash = hashlib.sha256(file_bytes).hexdigest()

    with conn.cursor() as cur:
        previous_hash = existing_content_hash(cur, source_type, source_id)
    conn.commit()
    if previous_hash == content_hash:
        return -1

    with conn.cursor() as cur:
        if previous_hash is not None:
            cur.execute(
                "delete from documents where source_type = %s and source_id = %s",
                (source_type, source_id),
            )

        cur.execute(
            """
            insert into documents
                (source_type, source_id, file_name, content_hash, category, root_category)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (source_type, source_id, file_name, content_hash, category, root_category),
        )
        document_id = cur.fetchone()[0]

        chunk_count = 0
        for page_number, (kind, data) in enumerate(pages, start=1):
            markdown_text = page_to_markdown(*data) if kind == "image" else data
            for chunk in chunk_text(markdown_text):
                if not chunk.strip():
                    continue
                embedding = embed(chunk)
                cur.execute(
                    """
                    insert into chunks (document_id, page, content, embedding)
                    values (%s, %s, %s, %s)
                    """,
                    (document_id, page_number, chunk, embedding),
                )
                chunk_count += 1

    conn.commit()
    return chunk_count
