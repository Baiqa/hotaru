"""Фаза 5 плана P0: группы фактов для синтеза (facts.collapse_facts)."""

from datetime import datetime, timedelta, timezone

from hotaru_worker import facts

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def row(n, source="A", group=None, bucket=None, source_count=None, relation=None, minutes=0, text=None, evidence=None):
    return {
        "claim_id": f"c{n}",
        "text": text or f"факт {n}",
        "evidence": evidence or f"цитата {n}",
        "type": "fact",
        "created_at": T0 + timedelta(minutes=minutes if minutes else n),
        "source_name": source,
        "source_url": f"https://example.org/{n}",
        "group_id": group,
        "bucket": bucket,
        "source_count": source_count,
        "relation": relation,
    }


def test_one_line_per_group_with_all_sources():
    rows = [
        row(1, "РИА", group="g1", bucket="confirmed", source_count=3),
        row(2, "ТАСС", group="g1", bucket="confirmed", source_count=3, relation="confirms"),
        row(3, "Интерфакс", group="g1", bucket="confirmed", source_count=3, relation="confirms"),
    ]
    result = facts.collapse_facts(rows)
    assert len(result) == 1
    assert result[0]["claim_id"] == "c1"  # самое раннее утверждение группы
    assert result[0]["source_name"] == "РИА, ТАСС, Интерфакс"
    assert result[0]["bucket"] == "confirmed"
    assert result[0]["source_count"] == 3


def test_evidence_of_every_member_is_kept_for_the_copy_check():
    rows = [
        row(1, group="g", bucket="confirmed", source_count=2, evidence="первая цитата"),
        row(2, "B", group="g", bucket="confirmed", source_count=2, relation="confirms", evidence="вторая цитата"),
    ]
    evidence = facts.collapse_facts(rows)[0]["evidence"]
    assert "первая цитата" in evidence and "вторая цитата" in evidence


def test_ungrouped_claims_are_separate_unique_facts():
    result = facts.collapse_facts([row(1), row(2), row(3)])
    assert [f["claim_id"] for f in result] == ["c1", "c2", "c3"]
    assert all(f["bucket"] == "unique" and f["source_count"] == 1 for f in result)


def test_cap_keeps_the_most_important_groups():
    rows = [row(n) for n in range(1, 61)]  # 60 одиночных фактов
    rows.append(row(100, "A", group="gc", bucket="confirmed", source_count=3, minutes=90))
    rows.append(row(101, "A", group="gd", bucket="disputed", source_count=2, minutes=91))
    rows.append(row(102, "B", group="gd", bucket="disputed", source_count=2, relation="contradicts", minutes=92))
    result = facts.collapse_facts(rows, limit=40)
    assert len(result) == 40
    ids = [f["claim_id"] for f in result]
    # спорная группа (обе стороны) и подтверждённая — раньше любых одиночных, хотя созданы позже
    assert ids[:3] == ["c101", "c102", "c100"]


def test_disputed_group_shows_both_sides():
    rows = [
        row(1, "A", group="g", bucket="disputed", source_count=2, text="ставка 16,5%"),
        row(2, "B", group="g", bucket="disputed", source_count=2, relation="contradicts", text="ставка 17%"),
    ]
    result = facts.collapse_facts(rows)
    assert [f["text"] for f in result] == ["ставка 16,5%", "ставка 17%"]
    assert {f["source_name"] for f in result[1:]} == {"B"}
    assert all(f["bucket"] == "disputed" for f in result)


def test_representative_is_not_a_contradicting_claim():
    rows = [
        row(1, "B", group="g", bucket="disputed", source_count=2, relation="contradicts"),
        row(2, "A", group="g", bucket="disputed", source_count=2, relation="confirms", minutes=5),
    ]
    assert facts.collapse_facts(rows)[0]["claim_id"] == "c2"


def test_source_names_are_not_repeated():
    rows = [
        row(1, "A", group="g", bucket="unique", source_count=1),
        row(2, "A", group="g", bucket="unique", source_count=1, relation="confirms"),
    ]
    assert facts.collapse_facts(rows)[0]["source_name"] == "A"


def test_empty():
    assert facts.collapse_facts([]) == []
