-- 検索範囲を絞るための分類（Notionのみ、Driveの特定サブフォルダのみ、など）
-- Driveは「指定フォルダの直下（1階層目）のサブフォルダ名」、Notionは固定で
-- 'Notion'を入れる。ルート直下に置かれたファイルやローカル登録ファイルは
-- NULL（「全体」検索でのみヒットする）
alter table documents add column if not exists category text;

create index if not exists documents_category_idx on documents (category);
