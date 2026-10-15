create extension if not exists vector;

-- 登録済みファイルの管理（重複登録防止用）
create table if not exists documents (
    id bigint generated always as identity primary key,
    source_type text not null,           -- 'drive_pdf' / 'notion' などの識別子
    source_id text not null,             -- Drive の fileId や Notion の pageIdなど
    file_name text not null,
    content_hash text,                   -- 再登録判定用のハッシュ値
    registered_at timestamptz not null default now(),
    unique (source_type, source_id)
);

-- チャンク本文とベクトル
create table if not exists chunks (
    id bigint generated always as identity primary key,
    document_id bigint not null references documents (id) on delete cascade,
    page integer,
    content text not null,
    embedding vector(512) not null,
    created_at timestamptz not null default now()
);

create index if not exists chunks_embedding_idx
    on chunks using hnsw (embedding vector_cosine_ops);
