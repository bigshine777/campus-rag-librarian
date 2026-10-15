-- チャットの質問・回答ログ（後から見返す・検索するための記録）
create table if not exists chat_log (
    id bigint generated always as identity primary key,
    question text not null,
    answer text not null,
    asked_at timestamptz not null default now()
);
