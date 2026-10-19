-- Фикс security advisor: function_search_path_mutable
alter function set_updated_at() set search_path = '';
