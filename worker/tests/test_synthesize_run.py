"""Фаза 5.2 плана P0: поведение прогона синтеза (synthesize.run_once) на подменённых БД и LLM."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as genai_errors

from hotaru_worker import config, db, quota, runlog, synthesis, synthesize


def event(n, sources=2):
    return {"id": f"e{n}", "title": f"событие {n}", "source_count": sources}


class Env:
    """Очередь отдаёт события из `queue`, учитывая exclude, как настоящая выборка."""

    def __init__(self, monkeypatch, queue, outcomes):
        self.queue = list(queue)
        self.outcomes = outcomes  # id события -> результат generate_render или исключение
        self.saved, self.recorded, self.generated, self.fetches = [], [], [], 0
        self.done = set()

        @contextmanager
        def fake_connect(url):
            yield object()

        monkeypatch.setattr(config, "load", lambda: SimpleNamespace(database_url="x", groq_api_key=None))
        monkeypatch.setattr(db, "connect", fake_connect)
        monkeypatch.setattr(synthesis, "build_client", lambda: object())
        monkeypatch.setattr(quota, "has_quota", lambda client, model: True)
        monkeypatch.setattr(runlog, "out_of_time", lambda: False)
        monkeypatch.setattr(db, "events_missing_render", self.missing)
        monkeypatch.setattr(db, "event_facts_for_synthesis", lambda conn, eid: [{"claim_id": "c"}])
        monkeypatch.setattr(synthesis, "generate_render", self.generate)
        monkeypatch.setattr(db, "save_render", lambda conn, eid, fmt, lang, body, model, claims: self.saved.append((eid, fmt, model)))
        monkeypatch.setattr(db, "record_render_result", lambda conn, eid, ok: self.recorded.append((eid, ok)))

    def missing(self, conn, limit, exclude=None):
        self.fetches += 1
        return [e for e in self.queue if e["id"] not in (exclude or []) and e["id"] not in self.done][:limit]

    def generate(self, client, event_id, facts, format):
        self.generated.append((event_id, format))
        outcome = self.outcomes.get(event_id, ("текст", [["c"]], "model-x"))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            self.done.add(event_id)
        return outcome


def api_error(code):
    return genai_errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


def run(monkeypatch, queue, outcomes=None):
    env = Env(monkeypatch, queue, outcomes or {})
    return env, synthesize.run_once()


def test_only_the_short_summary_is_generated_and_actual_model_is_saved(monkeypatch):
    env, stats = run(monkeypatch, [event(1)])
    assert env.generated == [("e1", "summary_30s")]
    assert env.saved == [("e1", "summary_30s", "model-x")]
    assert env.recorded == [("e1", True)]
    assert stats["renders_written"] == 1 and stats["events_processed"] == 1


def test_rejected_generation_counts_as_a_failed_attempt(monkeypatch):
    env, stats = run(monkeypatch, [event(1), event(2)], {"e1": None})
    assert ("e1", False) in env.recorded and ("e2", True) in env.recorded
    assert stats["failed_generation"] == 1


def test_unusable_model_answer_counts_as_a_failed_attempt(monkeypatch):
    env, _ = run(monkeypatch, [event(1), event(2)], {"e1": ValueError("мусор")})
    assert ("e1", False) in env.recorded


def test_network_or_server_failure_is_not_the_events_fault(monkeypatch):
    env, _ = run(monkeypatch, [event(1), event(2)], {"e1": httpx.ConnectError("сеть"), "e2": api_error(503)})
    assert env.recorded == []  # ни успехов, ни засчитанных неудач


def test_run_stops_when_a_whole_batch_produces_nothing(monkeypatch):
    env, stats = run(monkeypatch, [event(n) for n in range(1, 4)], {f"e{n}": httpx.ConnectError("x") for n in range(1, 4)})
    # без защиты внешний цикл перечитывал бы те же события бесконечно
    assert env.fetches <= 2
    assert env.generated.count(("e1", "summary_30s")) == 1


def test_failed_events_are_excluded_from_the_next_fetch_in_the_same_run(monkeypatch):
    queue = [event(1)] + [event(n) for n in range(2, 8)]
    env = Env(monkeypatch, queue, {"e1": httpx.ConnectError("x")})
    monkeypatch.setattr(synthesize, "BATCH_SIZE", 2)
    synthesize.run_once()
    assert env.generated.count(("e1", "summary_30s")) == 1
    assert {e for e, _ in env.recorded} == {f"e{n}" for n in range(2, 8)}


def test_quota_wall_stops_the_run(monkeypatch):
    env, stats = run(monkeypatch, [event(1), event(2), event(3)], {"e1": api_error(429)})
    assert env.generated == [("e1", "summary_30s")]
    assert stats["skipped_quota_exhausted"] is True
    assert env.recorded == []


def test_single_source_events_are_capped_per_run(monkeypatch):
    queue = [event(1), event(2)] + [event(n, sources=1) for n in range(3, 3 + synthesize.MAX_SINGLE_EVENTS_PER_RUN + 5)]
    env, stats = run(monkeypatch, queue)
    generated = [e for e, _ in env.generated]
    assert "e1" in generated and "e2" in generated  # события с 2+ изданиями — без потолка
    assert stats["single_events"] == synthesize.MAX_SINGLE_EVENTS_PER_RUN
    assert len(generated) == 2 + synthesize.MAX_SINGLE_EVENTS_PER_RUN


def test_event_without_facts_is_skipped_and_not_retried(monkeypatch):
    env = Env(monkeypatch, [event(1), event(2)], {})
    monkeypatch.setattr(db, "event_facts_for_synthesis", lambda conn, eid: [] if eid == "e1" else [{"claim_id": "c"}])
    synthesize.run_once()
    assert [e for e, _ in env.generated] == ["e2"]


def test_time_budget_ends_the_run(monkeypatch):
    env = Env(monkeypatch, [event(1), event(2)], {})
    monkeypatch.setattr(runlog, "out_of_time", lambda: True)
    synthesize.run_once()
    assert env.generated == []


# --- извлечение фактов: какие ошибки считаются «отравой» ----------------------------


@pytest.mark.parametrize("code,permanent", [(400, True), (404, True), (429, False), (500, False), (503, False)])
def test_only_permanent_client_errors_count_against_an_article(code, permanent):
    from hotaru_worker import extract_claims

    assert extract_claims._is_permanent_error(api_error(code)) is permanent
    assert extract_claims._is_permanent_error(httpx.ConnectError("x")) is False
