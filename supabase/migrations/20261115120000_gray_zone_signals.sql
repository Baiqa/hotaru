-- Фаза 4 плана P0 (tmp/plans/p0-ispravleniya-posle-audita.md): рядом с вердиктом LLM
-- серой зоны сохраняем итоговый скор пары и четыре сигнала, из которых он сложен.
-- Нужно для подбора порогов и весов на реальных вердиктах (P1): без этих чисел решение
-- LLM нельзя сопоставить со скором, по которому пара попала в серую зону.
-- У прежних строк значения останутся пустыми — их скор не сохраняли.

alter table gray_zone_verdicts
  add column score double precision,
  add column entity_jaccard double precision,
  add column tokens_jaccard double precision,
  add column cosine_sim double precision,
  add column time_decay double precision;
