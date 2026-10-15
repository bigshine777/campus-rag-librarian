-- 検索範囲の選択をメインフォルダ単位でもできるようにするための列
-- Driveは「指定したフォルダ（メインフォルダ）自体の表示名」、
-- Notionはcategory（'Notion'）と合わせるためNULLのままにする
-- （Notionに階層構造はないため、メインフォルダのグループには含めない）
alter table documents add column if not exists root_category text;

create index if not exists documents_root_category_idx on documents (root_category);
