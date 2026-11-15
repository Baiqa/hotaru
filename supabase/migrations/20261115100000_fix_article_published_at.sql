-- Фаза 3 плана P0 (tmp/plans/p0-ispravleniya-posle-audita.md): разовая правка
-- articles.published_at у уже собранных полнотекстовых статей.
--
-- Проблема: collect.py брал дату со страницы (trafilatura) и перекрывал ею точное
-- время из RSS. Дата со страницы — это сутки (у 6096 из 6096 статей ровно 00:00
-- UTC), а у 159 статей она ещё и неверна: у РИА и Интерфакса повторяются
-- заглушки 2023-01-01 (83 раза), 2026-11-01 (58), 2022-06-22 (5).
-- Новый код (dates.py) берёт время из RSS; эта миграция подтягивает старые записи.
--
-- Точное время старых статей восстановить нельзя, поэтому:
--   * дата правдоподобна — не раньше чем за 2 суток до сбора, ИЛИ подтверждена
--     датой в самом URL (Guardian /2026/sep/18/, РИА /20261115/; так выжили 14
--     законно старых материалов из «догоняющих» запусков) —
--     тогда оставляем сутки, но время берём не полночь, а
--     least(момент сбора, конец этих суток): верхняя оценка, ошибка в среднем
--     порядка часов (у неполных статей лента даёт задержку сбора p50 1,9 ч);
--   * иначе (дата — заглушка) время сбора.
-- Затрагиваются только строки с полуночной меткой — признаком даты страницы, поэтому
-- повторный запуск безопасен. Копия прежних значений: CSV в рабочей папке сессии
-- (id, published_at, created_at), 6096 строк, от 2026-11-15.

with c as (
  select id, created_at, canonical_url,
         (published_at at time zone 'UTC')::date as d,
         (created_at at time zone 'UTC')::date as cd
  from articles
  where fetch_status = 'ok'
    and (published_at at time zone 'UTC')::time = '00:00:00'
), k as (
  select id, created_at, d,
         (
           d between cd - 2 and cd
           or canonical_url like '%/' || to_char(d, 'YYYY') || '/' || lower(to_char(d, 'Mon')) || '/' || to_char(d, 'DD') || '/%'
           or canonical_url like '%/' || to_char(d, 'YYYYMMDD') || '/%'
         ) as keep_day
  from c
)
update articles a
set published_at = case
      when k.keep_day then least(k.created_at, (k.d::timestamp + interval '1 day' - interval '1 second') at time zone 'UTC')
      else k.created_at
    end
from k
where a.id = k.id;
