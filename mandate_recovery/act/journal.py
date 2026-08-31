"""Append-only, hash-chained, crash-safe write-ahead log.

Three properties, each of which exists because of a specific failure:

**Durable before the effect.** Every append is flushed and `fsync`ed before the
call returns. Without this, a decision can be executed against the gateway and
then lost from the log by a power failure, leaving an effect nobody can account
for.

**Hash-chained.** Each record carries the hash of its predecessor, so the log is
tamper-evident and a reader can prove it is reading the same history the writer
wrote. Verification runs on open, not on demand.

**Torn-tail tolerant.** A process killed with `SIGKILL` mid-write leaves a
partial final line. That is normal and must not prevent startup: recovery
truncates the incomplete tail. A record that fails verification anywhere *other*
than the tail is corruption rather than a torn write, and refuses to load.

The file format is one JSON object per line — greppable, diffable, and readable
by anyone reviewing an incident without a special tool.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, Iterator

GENESIS_HASH: Final[str] = "0" * 64


class RecordKind(Enum):
    """What a journal record represents.

    `INTENT` and `EFFECT` are the two halves of the two-phase commit in
    `executor.py`. An `INTENT` with no matching `EFFECT` is the in-doubt window,
    and resolving it is what crash recovery is for.
    """

    #: A webhook accepted at the edge. Recorded in the same hash chain as
    #: decisions and effects, so the provenance of a failure and the
    #: decision it led to sit in one verifiable log.
    INGEST = "INGEST"
    RUN_START = "RUN_START"
    DECISION = "DECISION"
    INTENT = "INTENT"
    EFFECT = "EFFECT"
    SKIPPED = "SKIPPED"
    KILL = "KILL"
    RUN_END = "RUN_END"


class TamperError(RuntimeError):
    """The hash chain is broken somewhere other than the final record.

    Deliberately not recoverable. A journal that has been edited cannot be used
    to prove what happened, and silently repairing it would destroy the only
    evidence that it was edited.
    """


@dataclass(frozen=True, slots=True)
class Record:
    seq: int
    kind: RecordKind
    ts: str
    run_id: str
    body: dict[str, Any]
    prev_hash: str
    hash: str

    def payload(self) -> dict[str, Any]:
        """The hashed portion. Excludes `hash` itself, includes everything else."""
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "ts": self.ts,
            "run_id": self.run_id,
            "body": self.body,
            "prev_hash": self.prev_hash,
        }

    def to_line(self) -> str:
        return _canonical({**self.payload(), "hash": self.hash}) + "\n"

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> "Record":
        return cls(
            seq=obj["seq"],
            kind=RecordKind(obj["kind"]),
            ts=obj["ts"],
            run_id=obj["run_id"],
            body=obj["body"],
            prev_hash=obj["prev_hash"],
            hash=obj["hash"],
        )


def _canonical(obj: dict[str, Any]) -> str:
    """Byte-stable JSON. Key order and separators are fixed, because the hash is
    computed over this text and must not depend on dict iteration order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What `Journal.open` found. Surfaced rather than logged, because a
    truncated tail means a previous process died and the operator should know."""

    records_loaded: int
    torn_bytes_discarded: int
    last_hash: str

    @property
    def recovered_from_crash(self) -> bool:
        return self.torn_bytes_discarded > 0


class Journal:
    """An append-only hash-chained log backed by one file.

    A single journal holds every run. That is deliberate: idempotency has to
    survive a restart, so the ledger of what has already been applied must be
    derivable from a log that spans runs.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self._report: RecoveryReport | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> RecoveryReport:
        """Load and verify the existing journal, truncating a torn tail.

        Safe to call on a path that does not exist yet.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._report = RecoveryReport(0, 0, GENESIS_HASH)
            return self._report

        raw = self.path.read_bytes()
        offset = 0
        count = 0
        prev_hash = GENESIS_HASH
        expected_seq = 0

        for line in raw.splitlines(keepends=True):
            valid, rec = self._parse_and_verify(line, prev_hash, expected_seq)
            if not valid:
                break
            assert rec is not None
            offset += len(line)
            prev_hash = rec.hash
            expected_seq += 1
            count += 1

        discarded = len(raw) - offset
        if discarded > 0:
            # Anything after the last good record must be a torn final write. If
            # it contains a complete, well-formed line then the log was edited
            # rather than interrupted, and that is not something to repair.
            tail = raw[offset:]
            if tail.endswith(b"\n"):
                raise TamperError(
                    f"{self.path}: hash chain broken at record {expected_seq}; "
                    f"{discarded} trailing bytes form a complete line, so this is "
                    f"not a torn write"
                )
            _truncate(self.path, offset)

        self._seq = expected_seq
        self._last_hash = prev_hash
        self._report = RecoveryReport(count, discarded, prev_hash)
        return self._report

    @staticmethod
    def _parse_and_verify(
        line: bytes, prev_hash: str, expected_seq: int
    ) -> tuple[bool, Record | None]:
        if not line.endswith(b"\n"):
            return False, None            # torn write: no terminating newline
        try:
            obj = json.loads(line)
            rec = Record.from_obj(obj)
        except (json.JSONDecodeError, KeyError, ValueError):
            return False, None
        if rec.seq != expected_seq or rec.prev_hash != prev_hash:
            return False, None
        if compute_hash(rec.payload()) != rec.hash:
            return False, None
        return True, rec

    @property
    def report(self) -> RecoveryReport:
        if self._report is None:
            raise RuntimeError("journal not opened")
        return self._report

    # -- writing -----------------------------------------------------------

    def append(
        self, kind: RecordKind, run_id: str, ts: str, body: dict[str, Any]
    ) -> Record:
        """Append one record and `fsync` before returning.

        The fsync is the entire point. An append that returns before the bytes
        are on the platter is a promise the log cannot keep.
        """
        payload = {
            "seq": self._seq,
            "kind": kind.value,
            "ts": ts,
            "run_id": run_id,
            "body": body,
            "prev_hash": self._last_hash,
        }
        rec = Record(
            seq=self._seq,
            kind=kind,
            ts=ts,
            run_id=run_id,
            body=body,
            prev_hash=self._last_hash,
            hash=compute_hash(payload),
        )
        with open(self.path, "ab") as fh:
            fh.write(rec.to_line().encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        self._seq += 1
        self._last_hash = rec.hash
        return rec

    # -- reading -----------------------------------------------------------

    def __iter__(self) -> Iterator[Record]:
        """Every record, verified. Raises `TamperError` on a broken chain.

        Reading verifies rather than trusting, because the value of the chain is
        entirely in it being checked.
        """
        if not self.path.exists():
            return
        prev_hash = GENESIS_HASH
        expected_seq = 0
        with open(self.path, "rb") as fh:
            for line in fh:
                valid, rec = self._parse_and_verify(line, prev_hash, expected_seq)
                if not valid:
                    if line.endswith(b"\n"):
                        raise TamperError(
                            f"{self.path}: hash chain broken at record {expected_seq}"
                        )
                    return                       # torn tail; nothing more to read
                assert rec is not None
                yield rec
                prev_hash = rec.hash
                expected_seq += 1

    def records_for(self, run_id: str) -> Iterator[Record]:
        return (r for r in self if r.run_id == run_id)

    def run_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self:
            seen.setdefault(r.run_id, None)
        return list(seen)

    def verify(self) -> int:
        """Walk the whole chain, returning the record count. Raises on tamper."""
        return sum(1 for _ in self)

    @property
    def head_hash(self) -> str:
        return self._last_hash

    @property
    def next_seq(self) -> int:
        return self._seq


def _truncate(path: Path, size: int) -> None:
    """Truncate and fsync, so recovery itself is durable.

    Without the fsync, a second crash immediately after recovery could resurrect
    the torn bytes that were just discarded.
    """
    with open(path, "r+b") as fh:
        fh.truncate(size)
        fh.flush()
        os.fsync(fh.fileno())
