"""AI Librarianのチャット画面。共有パスワードでログインしてから質問できる
使い方: streamlit run scripts/app.py
"""

import hashlib
import hmac
import os
import time

import streamlit as st
import streamlit.components.v1 as components

from ask import answer, research, source_url
from db import connect

st.set_page_config(page_title="AI Librarian", initial_sidebar_state="expanded")

st.markdown(
    "<style>.stMainBlockContainer { padding-top: 3rem; }</style>",
    unsafe_allow_html=True,
)

COOKIE_NAME = "ai_librarian_session"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _sign(payload: str) -> str:
    key = os.environ["APP_PASSWORD"].encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    expires_at = str(int(time.time()) + COOKIE_MAX_AGE_SECONDS)
    return f"{expires_at}.{_sign(expires_at)}"


def is_valid_session_token(token: str) -> bool:
    expires_at, _, signature = token.partition(".")
    if not expires_at or not signature:
        return False
    if not hmac.compare_digest(_sign(expires_at), signature):
        return False
    return int(expires_at) > time.time()


def set_session_cookie(token: str) -> None:
    components.html(
        f"<script>document.cookie = "
        f"'{COOKIE_NAME}={token}; max-age={COOKIE_MAX_AGE_SECONDS}; path=/';</script>",
        height=0,
    )


def require_login() -> None:
    token = st.context.cookies.get(COOKIE_NAME)
    if token and is_valid_session_token(token):
        st.session_state.authenticated = True

    if st.session_state.get("authenticated"):
        return

    login_container = st.empty()
    with login_container.container():
        st.title("AI Librarian")
        with st.form("login_form"):
            password = st.text_input("パスワード", type="password")
            submitted = st.form_submit_button("ログイン")

    if submitted:
        if password == os.environ["APP_PASSWORD"]:
            new_token = make_session_token()
            st.session_state.authenticated = True
            set_session_cookie(new_token)
            login_container.empty()
            return
        else:
            st.error("パスワードが違います")
    st.stop()


def log_chat(conn, question: str, answer_text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into chat_log (question, answer) values (%s, %s)",
            (question, answer_text),
        )
    conn.commit()


def render_history_sidebar() -> None:
    with st.sidebar:
        st.subheader("履歴")
        search_query = st.text_input("検索", key="history_search")

        with st.spinner("検索中..."):
            with connect() as conn:
                with conn.cursor() as cur:
                    if search_query:
                        cur.execute(
                            """
                            select question, answer, asked_at from chat_log
                            where question ilike %s or answer ilike %s
                            order by asked_at desc limit 50
                            """,
                            (f"%{search_query}%", f"%{search_query}%"),
                        )
                    else:
                        cur.execute(
                            "select question, answer, asked_at from chat_log "
                            "order by asked_at desc limit 20"
                        )
                    rows = cur.fetchall()

        if not rows:
            st.caption("履歴はまだありません")

        for question, answer_text, asked_at in rows:
            label = f"{asked_at:%Y-%m-%d %H:%M} {question[:20]}"
            with st.expander(label):
                st.write(f"**Q:** {question}")
                st.write(f"**A:** {answer_text}")


ALL_SCOPE_LABEL = "全体"


def render_scope_selector() -> tuple[str | None, str | None]:
    with st.sidebar:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select distinct root_category, category from documents "
                    "where root_category is not null or category is not null "
                    "order by root_category, category"
                )
                rows = cur.fetchall()

        roots: list[str] = []
        sub_by_root: dict[str, list[str]] = {}
        standalone_categories: list[str] = []
        for root_cat, cat in rows:
            if root_cat:
                if root_cat not in roots:
                    roots.append(root_cat)
                if cat:
                    sub_by_root.setdefault(root_cat, []).append(cat)
            elif cat and cat not in standalone_categories:
                standalone_categories.append(cat)

        selected_root = st.pills(
            "検索範囲",
            [ALL_SCOPE_LABEL, *roots, *standalone_categories],
            default=ALL_SCOPE_LABEL,
            key="scope_root",
        )

        if selected_root in (None, ALL_SCOPE_LABEL):
            return None, None
        if selected_root in standalone_categories:
            return selected_root, None

        subs = sub_by_root.get(selected_root, [])
        if not subs:
            return None, selected_root

        root_whole_label = f"{selected_root}（全体）"
        selected_sub = st.pills(
            "サブフォルダ",
            [root_whole_label, *subs],
            default=root_whole_label,
            key=f"scope_sub_{selected_root}",
        )
        if selected_sub in (None, root_whole_label):
            return None, selected_root
        return selected_sub, selected_root


def main() -> None:
    require_login()

    scope_category, scope_root_category = render_scope_selector()
    render_history_sidebar()

    st.title("AI Librarian")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    chat_value = st.chat_input(
        "学内資料について質問してください",
        accept_file=True,
        file_type=["png", "jpg", "jpeg", "webp"],
    )
    if not chat_value:
        return

    question = chat_value.text
    images = [(f.getvalue(), f.type) for f in chat_value.files]
    if not question and not images:
        return
    if not question:
        question = "この画像について教えてください。"

    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        for image_bytes, _mime_type in images:
            st.image(image_bytes, width=200)
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("資料を検索中..."):
            with connect() as conn:
                contexts = (
                    research(
                        conn,
                        question,
                        category=scope_category,
                        root_category=scope_root_category,
                    )
                    if question
                    else []
                )

        if not contexts and not images:
            response = "資料が見つかりませんでした。まだ何も登録されていないかもしれません。"
        else:
            with st.spinner("回答を作成中..."):
                response = answer(question, contexts, images=images or None)

        st.write(response)
        if contexts:
            with st.expander("出典"):
                seen = set()
                for _chunk_id, name, page, _content, source_type, source_id in contexts:
                    label = f"{name} p.{page}"
                    if label in seen:
                        continue
                    seen.add(label)

                    url = source_url(source_type, source_id)
                    if url:
                        st.write(f"[{label}]({url})")
                    else:
                        st.write(label)

    st.session_state.messages.append({"role": "assistant", "content": response})

    with connect() as conn:
        log_chat(conn, question, response)


if __name__ == "__main__":
    main()
