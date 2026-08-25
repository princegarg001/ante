"""The write-ahead log.

A journal that loses a record loses the only evidence of an effect, and a
journal that refuses to open after a crash turns a recoverable incident into an
outage. Both failure modes are tested here directly rather than inferred.
"""

from __future__ import annotations

import json

import pytest

from mandate_recovery.act.journal import (
    GENESIS_HASH,
    Journal,
    RecordKind,
    TamperError,
    compute_hash,
)

TS = "2026-09-01T00:00:00+05:30"


def make_journal(tmp_path, n: int = 3) -> Journal:
    j = Journal(tmp_path / "journal.jsonl")
    j.open()
    for i in range(n):
        j.append(RecordKind.DECISION, "run-1", TS, {"i": i})
    return j


# --------------------------------------------------------------------------- #
# Basics
# --------------------------------------------------------------------------- #


def test_opening_a_missing_journal_is_not_an_error(tmp_path) -> None:
    j = Journal(tmp_path / "nope.jsonl")
    report = j.open()
    assert report.records_loaded == 0
    assert report.last_hash == GENESIS_HASH
    assert not report.recovered_from_crash


def test_append_and_read_roundtrip(tmp_path) -> None:
    j = make_journal(tmp_path, 5)
    records = list(j)
    assert [r.body["i"] for r in records] == [0, 1, 2, 3, 4]
    assert [r.seq for r in records] == [0, 1, 2, 3, 4]


def test_chain_links_each_record_to_its_predecessor(tmp_path) -> None:
    j = make_journal(tmp_path, 4)
    records = list(j)
    assert records[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(records, records[1:]):
        assert cur.prev_hash == prev.hash


def test_reopening_resumes_the_chain(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    first = Journal(path)
    first.open()
    for i in range(3):
        first.append(RecordKind.DECISION, "run-1", TS, {"i": i})
    head, seq = first.head_hash, first.next_seq

    second = Journal(path)
    report = second.open()
    assert report.records_loaded == 3
    assert second.head_hash == head
    assert second.next_seq == seq

    second.append(RecordKind.DECISION, "run-2", TS, {"i": 99})
    assert second.verify() == 4


def test_data_is_on_disk_before_append_returns(tmp_path) -> None:
    """The fsync is the entire promise of a write-ahead log. If append can return
    before the bytes are durable, an effect can outlive its own record."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.open()
    j.append(RecordKind.INTENT, "run-1", TS, {"idem_key": "abc"})
    # Read through a completely separate handle — no buffer of ours involved.
    assert "abc" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Crash: a torn final write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tail", [b'{"seq": 3, "kind": "DEC', b"{", b'{"seq":3,"kind":"DECISION"}']
)
def test_torn_tail_is_truncated_not_fatal(tmp_path, tail: bytes) -> None:
    """SIGKILL mid-write leaves a partial line. That is normal operation for a
    WAL and must not prevent startup."""
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 3)
    with open(path, "ab") as fh:
        fh.write(tail)                      # no trailing newline: a torn write

    reopened = Journal(path)
    report = reopened.open()

    assert report.recovered_from_crash
    assert report.torn_bytes_discarded == len(tail)
    assert report.records_loaded == 3
    assert reopened.verify() == 3


def test_recovery_is_itself_durable(tmp_path) -> None:
    """Truncation is fsynced, so a second crash straight after recovery cannot
    resurrect the bytes that were just discarded."""
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 2)
    with open(path, "ab") as fh:
        fh.write(b'{"partial')

    Journal(path).open()
    assert not path.read_text(encoding="utf-8").endswith('{"partial')

    again = Journal(path)
    assert again.open().torn_bytes_discarded == 0


def test_writing_continues_cleanly_after_recovery(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 2)
    with open(path, "ab") as fh:
        fh.write(b'{"torn')

    j = Journal(path)
    j.open()
    j.append(RecordKind.EFFECT, "run-1", TS, {"idem_key": "k"})
    records = list(j)
    assert len(records) == 3
    assert records[-1].seq == 2
    assert records[-1].prev_hash == records[-2].hash


# --------------------------------------------------------------------------- #
# Tamper: not the same thing as a crash
# --------------------------------------------------------------------------- #


def test_edited_record_is_refused(tmp_path) -> None:
    """A journal that has been edited cannot prove what happened, and silently
    repairing it would destroy the evidence that it was edited."""
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 4)

    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[1])
    obj["body"]["i"] = 999
    lines[1] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TamperError):
        Journal(path).open()


def test_a_forged_but_internally_consistent_record_is_still_refused(tmp_path) -> None:
    """Recomputing the record's own hash is not enough — it must also chain to
    the record before it, which an attacker cannot fix without rewriting the
    entire suffix."""
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 4)

    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[2])
    obj["body"]["i"] = 42
    obj["hash"] = compute_hash({k: v for k, v in obj.items() if k != "hash"})
    lines[2] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TamperError):
        Journal(path).open()


def test_a_complete_trailing_line_is_tamper_not_a_torn_write(tmp_path) -> None:
    """The distinction that makes torn-tail recovery safe: a torn write cannot
    end in a newline, so anything that does was appended deliberately."""
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 2)
    with open(path, "ab") as fh:
        fh.write(b'{"seq":2,"kind":"DECISION","hash":"deadbeef"}\n')

    with pytest.raises(TamperError, match="not a torn write"):
        Journal(path).open()


def test_deleting_a_record_from_the_middle_is_detected(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    make_journal(tmp_path, 5)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TamperError):
        Journal(path).open()


# --------------------------------------------------------------------------- #
# Determinism of the chain
# --------------------------------------------------------------------------- #


def test_hash_is_stable_across_key_ordering(tmp_path) -> None:
    """The chain is computed over canonical JSON, so it cannot depend on dict
    iteration order — otherwise a log written by one process would fail to
    verify in another."""
    a = compute_hash({"seq": 0, "kind": "X", "body": {"b": 1, "a": 2}})
    b = compute_hash({"kind": "X", "body": {"a": 2, "b": 1}, "seq": 0})
    assert a == b
