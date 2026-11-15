"""Фаза 5.5 плана P0: пакетные эмбеддинги — как SDK получает тексты (без обращений к API)."""

import pytest
from google.genai import types

from hotaru_worker import embeddings


class _Emb:
    def __init__(self, values):
        self.values = values


class FakeClient:
    def __init__(self, n_returned=None):
        self.calls = []
        self.n_returned = n_returned
        self.models = self

    def embed_content(self, model, contents, config):
        self.calls.append(contents)
        n = self.n_returned if self.n_returned is not None else len(contents)
        return type("R", (), {"embeddings": [_Emb([float(i)]) for i in range(n)]})()


def test_each_text_becomes_its_own_content():
    # Список строк SDK склеивает в один контент (один вектор на всё) — проверено на API.
    client = FakeClient()
    vectors = embeddings.embed_texts(client, ["а", "б", "в"])
    assert len(vectors) == 3
    contents = client.calls[0]
    assert len(contents) == 3 and all(isinstance(c, types.Content) for c in contents)
    assert [c.parts[0].text for c in contents] == ["а", "б", "в"]


def test_wrong_number_of_vectors_is_an_error_not_silent_misalignment():
    with pytest.raises(ValueError):
        embeddings.embed_texts(FakeClient(n_returned=1), ["а", "б", "в"])
