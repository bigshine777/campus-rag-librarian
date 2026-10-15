"""質問を受け取り、Supabaseで検索した本文をもとにAzureで回答する
使い方: python scripts/ask.py "質問文"
"""

import base64
import os
import re
import sys

from openai import AzureOpenAI

from db import connect

TOP_K = 5
MAX_OUTPUT_TOKENS = 500
KEYWORD_MAX_EXTRA = 3
PROFILE_NAME_KEYWORD_LIMIT = 10

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
)
EMBEDDING_DEPLOYMENT = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
EMBEDDING_DIMENSIONS = int(os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"])
CHAT_DEPLOYMENT = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]
USER_PROFILE = os.environ.get("APP_USER_PROFILE", "")


def extract_profile_name(profile: str) -> str | None:
    match = re.search(r"氏名[:：]\s*([^\s。、]+)", profile)
    return match.group(1) if match else None


def embed(text: str) -> list[float]:
    response = client.embeddings.create(
        model=EMBEDDING_DEPLOYMENT,
        input=text,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


def search(
    conn,
    query_embedding: list[float],
    top_k: int = TOP_K,
    category: str | None = None,
    root_category: str | None = None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.id, d.file_name, c.page, c.content, d.source_type, d.source_id
            from chunks c
            join documents d on d.id = c.document_id
            where (%s::text is null or d.category = %s)
              and (%s::text is null or d.root_category = %s)
            order by c.embedding <=> %s::vector
            limit %s
            """,
            (category, category, root_category, root_category, query_embedding, top_k),
        )
        return cur.fetchall()


def extract_keywords(question: str, max_keywords: int = 5) -> list[str]:
    pattern = r"[一-龠々ー]{2,}|[ァ-ヴー]{2,}|[A-Za-z]+(?:\s+[A-Za-z]+){0,3}"
    keywords = list(dict.fromkeys(re.findall(pattern, question)))
    return keywords[:max_keywords]


def keyword_search(
    conn,
    keywords: list[str],
    exclude_chunk_ids: set[int],
    limit: int = KEYWORD_MAX_EXTRA,
    category: str | None = None,
    root_category: str | None = None,
):
    if not keywords:
        return []

    seen_ids = set(exclude_chunk_ids)
    matches_per_keyword: list[list[tuple]] = []
    per_keyword_fetch = limit + 2
    with conn.cursor() as cur:
        for keyword in keywords:
            cur.execute(
                """
                select id, file_name, page, content, source_type, source_id
                from (
                    select c.id, d.file_name, c.page, c.content, d.source_type, d.source_id,
                           row_number() over (partition by d.id order by c.id) as rn
                    from chunks c
                    join documents d on d.id = c.document_id
                    where (replace(replace(c.content, ' ', ''), '　', '')
                          ilike '%%' || replace(replace(%s, ' ', ''), '　', '') || '%%')
                      and (%s::text is null or d.category = %s)
                      and (%s::text is null or d.root_category = %s)
                ) ranked
                where rn <= 2
                order by rn, file_name
                limit %s
                """,
                (keyword, category, category, root_category, root_category, per_keyword_fetch),
            )
            matches_per_keyword.append(
                [row for row in cur.fetchall() if row[0] not in seen_ids]
            )

    matches_per_keyword.sort(key=len)

    extra = []
    for matches in matches_per_keyword:
        for row in matches:
            if row[0] not in seen_ids:
                seen_ids.add(row[0])
                extra.append(row)
                if len(extra) >= limit:
                    return extra

    return extra


def keyword_matches(
    conn,
    question: str,
    exclude_chunk_ids: set[int],
    category: str | None = None,
    root_category: str | None = None,
) -> list[tuple]:
    keywords = extract_keywords(question)
    profile_name = None
    if USER_PROFILE and any(pronoun in question for pronoun in ("私", "俺", "自分")):
        profile_name = extract_profile_name(USER_PROFILE)

    if not profile_name:
        return keyword_search(
            conn, keywords, exclude_chunk_ids, category=category, root_category=root_category
        )

    seen_ids = set(exclude_chunk_ids)
    name_results = keyword_search(
        conn,
        [profile_name],
        seen_ids,
        limit=PROFILE_NAME_KEYWORD_LIMIT,
        category=category,
        root_category=root_category,
    )
    seen_ids |= {row[0] for row in name_results}
    other_results = keyword_search(
        conn, keywords, seen_ids, category=category, root_category=root_category
    )
    return name_results + other_results


def combined_search(
    conn,
    question: str,
    query_embedding: list[float],
    top_k: int = TOP_K,
    category: str | None = None,
    root_category: str | None = None,
):
    vector_results = search(conn, query_embedding, top_k, category=category, root_category=root_category)
    existing_ids = {row[0] for row in vector_results}
    extra_results = keyword_matches(
        conn, question, existing_ids, category=category, root_category=root_category
    )
    return extra_results + vector_results


FOLLOWUP_MAX_TOKENS = 50
NO_NEW_ENTITY_MARKER = "なし"


def find_new_entity(question: str, contexts) -> str | None:
    if not contexts:
        return None

    context_text = "\n\n".join(
        f"[出典: {file_name} p.{page}]\n{content}"
        for _chunk_id, file_name, page, content, _source_type, _source_id in contexts
    )
    system_content = (
        "資料と質問を読み、質問に答える上で鍵となる固有名詞"
        "（人物名など）で、質問文自体には書かれておらず資料を"
        "読んで初めて判明したものを1つだけ挙げてください。\n"
        "例：質問が「私のアドバイザーの面談日程は？」で、資料中の"
        "利用者プロフィールから質問者の氏名が分かり、さらに名簿"
        "からその人のアドバイザー名が「John Smith」だと分かった"
        "場合、出力は「John Smith」のみ。\n"
        "資料には同じ種類の行（学生とアドバイザーの対応表など）が"
        "複数含まれることがあります。質問者本人（「私/俺/自分」が"
        "誰を指すか）に直接対応する行だけを見て、それ以外の無関係な"
        "行（他の学生・他の人物の行）を固有名詞として抽出しないで"
        "ください。\n"
        "そのような新しい固有名詞がなければ"
        f"「{NO_NEW_ENTITY_MARKER}」とだけ出力してください。"
        "出力は固有名詞そのもの、または"
        f"「{NO_NEW_ENTITY_MARKER}」のみとし、"
        "説明や句点を含めないでください。"
    )
    if USER_PROFILE and any(pronoun in question for pronoun in ("私", "俺", "自分")):
        system_content += f"\n\n質問者本人のプロフィール: {USER_PROFILE}"

    response = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        max_completion_tokens=FOLLOWUP_MAX_TOKENS,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": f"資料:\n{context_text}\n\n質問: {question}",
            },
        ],
    )
    result = (response.choices[0].message.content or "").strip()
    if not result or NO_NEW_ENTITY_MARKER in result:
        return None
    return result


def research(
    conn,
    question: str,
    category: str | None = None,
    root_category: str | None = None,
) -> list[tuple[int, str, int, str, str, str]]:
    query_embedding = embed(question)
    vector_results = search(conn, query_embedding, category=category, root_category=root_category)
    keyword_results = keyword_matches(
        conn,
        question,
        {row[0] for row in vector_results},
        category=category,
        root_category=root_category,
    )
    contexts = keyword_results + vector_results

    new_entity = find_new_entity(question, keyword_results or contexts)
    if new_entity:
        existing_ids = {row[0] for row in contexts}

        followup_query = f"{new_entity} {question}"
        followup_embedding = embed(followup_query)
        vector_results = search(
            conn, followup_embedding, category=category, root_category=root_category
        )

        seen_ids = existing_ids | {row[0] for row in vector_results}
        entity_results = keyword_search(
            conn, [new_entity], seen_ids, category=category, root_category=root_category
        )
        seen_ids |= {row[0] for row in entity_results}
        other_results = keyword_search(
            conn,
            extract_keywords(question),
            seen_ids,
            category=category,
            root_category=root_category,
        )

        followup_contexts = vector_results + entity_results + other_results
        contexts = contexts + [
            row for row in followup_contexts if row[0] not in existing_ids
        ]

    return contexts


def source_url(source_type: str, source_id: str) -> str | None:
    if source_type in ("drive_pdf", "drive_file"):
        return f"https://drive.google.com/file/d/{source_id}/view"
    if source_type == "notion":
        return f"https://www.notion.so/{source_id.replace('-', '')}"
    return None


def answer(
    question: str,
    contexts: list[tuple[int, str, int, str, str, str]],
    images: list[tuple[bytes, str]] | None = None,
) -> str:
    context_text = "\n\n".join(
        f"出典ファイル名: {file_name}　ページ: {page}"
        + (f"　URL: {url}" if (url := source_url(source_type, source_id)) else "")
        + f"\n{content}"
        for _chunk_id, file_name, page, content, source_type, source_id in contexts
    )
    system_content = (
        "あなたは学内資料を検索して回答するアシスタントです。"
        "以下の資料の内容だけを根拠に、日本語で回答してください。"
        "資料に書かれていないことは「資料からは分かりません」と答えてください。\n\n"
        "出典の示し方: 回答の各主張の直後に、その根拠となった資料への"
        "出典を必ず挿入してください。資料に「リンク」が記載されている場合は"
        "Markdownリンク構文 `[file_name p.X](リンク)` （必ず角括弧+丸括弧で"
        "URLを囲む）、リンクがない場合は `(file_name p.X)` の形式で、"
        "文末に挿入してください。\n"
        "URLが分かっている資料は、必ずMarkdownリンクにしてください"
        "（省略して括弧書きだけにしないでください）。\n"
        "例（URLが分かっている場合）: 前期の授業期間は4月8日〜7月22日です。"
        "[academic_calendar_ja.pdf p.1](https://drive.google.com/file/d/xxx/view)\n"
        "例（URLが分からない場合）: 前期の授業期間は4月8日〜7月22日です。"
        "(academic_calendar_ja.pdf p.1)"
        "出典だけの文は書かず、既存の文の末尾に挿入する形にしてください。"
        "複数の資料をまたぐ箇所は、該当する出典を全部続けて書いてください。"
        "資料ごとに出典をまとめた一覧は末尾に書かず、必ず各主張の直後に"
        "挿入してください（末尾の出典一覧は別途UI側で表示するため不要です）。"
    )
    if USER_PROFILE:
        system_content += (
            f"\n\nこのアプリの利用者はただ一人で、次の人物です: {USER_PROFILE}\n"
            "質問文の「私」「俺」「自分」は全てこの人物を指します。"
        )
    user_text = f"資料:\n{context_text}\n\n質問: {question}"
    if images:
        user_content = [{"type": "text", "text": user_text}]
        for image_bytes, mime_type in images:
            b64 = base64.b64encode(image_bytes).decode()
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                }
            )
    else:
        user_content = user_text

    response = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
    )
    return fix_inverted_markdown_links(response.choices[0].message.content)


def fix_inverted_markdown_links(text: str) -> str:
    return re.sub(r"\(([^()\[\]]+)\)\[(https?://[^\]]+)\]", r"[\1](\2)", text)


def main() -> None:
    if len(sys.argv) < 2:
        print('使い方: python scripts/ask.py "質問文"')
        return

    question = sys.argv[1]

    with connect() as conn:
        contexts = research(conn, question)

    if not contexts:
        print("NG: 検索結果がありません（まだ何も登録されていないかもしれません）")
        return

    print(answer(question, contexts))


if __name__ == "__main__":
    main()
