"""Streaming, memory-bounded parsing for weekly import request bodies.

A full week batch can hold up to 200k records.  Buffering the whole body and
decoding it into Python objects (plus one validated model per record) does
not fit into the small address space this service runs in, so the body is
consumed as an async byte stream instead:

* chunks are appended to a small, compacted buffer — the body is never held
  in memory at once;
* each record is validated as it arrives and aggregated straight into the
  fixed street/age-band cells, then discarded;
* raw patient identifiers live only in a local de-duplication set for the
  duration of the parse.  They are never written to the database, a file, or
  a log, and the set is released as soon as parsing finishes.

Only after the *entire* body has validated does the caller persist anything,
so a duplicate identifier, an unknown group, or a malformed trailing field
anywhere in the batch rejects the whole import with nothing left behind.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from .errors import (
    DuplicatePatientError,
    InvalidImportPayloadError,
    PayloadTooLargeError,
)
from .groups import AGE_BANDS, STREETS
from .schemas import IMPORT_MAX_RECORDS, PATIENT_ID_MAX_LENGTH, validate_iso_week

# Hard cap on the request body.  The largest legitimate payload (200k records
# with 200-character patient identifiers) is roughly 55 MiB, so 64 MiB leaves
# comfortable headroom while still bounding what a single token can force the
# parser to buffer.
IMPORT_MAX_BODY_BYTES = 64 * 1024 * 1024

_STREET_SET = frozenset(STREETS)
_AGE_BAND_SET = frozenset(AGE_BANDS)

_WS = b" \t\r\n"
# Characters that may appear in an unquoted JSON token (number or literal).
_BARE_TOKEN_CHARS = b"0123456789+-.eEtruefalsn"

_QUOTE = 0x22  # "
_BACKSLASH = 0x5C  # \
_OPEN_BRACE = 0x7B  # {
_CLOSE_BRACE = 0x7D  # }
_OPEN_BRACKET = 0x5B  # [
_CLOSE_BRACKET = 0x5D  # ]
_COLON = 0x3A  # :
_COMMA = 0x2C  # ,


class _BodyReader:
    """Incremental buffered reader over an async chunk stream.

    Consumed bytes are discarded whenever a new chunk is pulled, so the
    buffer stays small unless a single JSON token forces it to grow (and
    even that is bounded by the body cap).
    """

    __slots__ = ("_chunks", "_buffer", "_pos", "_eof", "_seen", "_max_bytes")

    def __init__(self, chunks: AsyncIterator[bytes], max_bytes: int) -> None:
        self._chunks = chunks
        self._buffer = bytearray()
        self._pos = 0
        self._eof = False
        self._seen = 0
        self._max_bytes = max_bytes

    async def _fill(self) -> bool:
        """Append the next chunk; return False once the body is exhausted."""

        if self._eof:
            return False
        chunk = await anext(self._chunks, None)
        if chunk is None:
            self._eof = True
            return False
        if self._pos:
            del self._buffer[: self._pos]
            self._pos = 0
        self._buffer += chunk
        self._seen += len(chunk)
        if self._seen > self._max_bytes:
            raise PayloadTooLargeError(
                f"the import body exceeds the {self._max_bytes}-byte limit"
            )
        return True

    async def next_byte(self) -> int:
        """Return the next byte, or -1 at the end of the body."""

        while self._pos >= len(self._buffer):
            if not await self._fill():
                return -1
        byte = self._buffer[self._pos]
        self._pos += 1
        return byte

    def unread(self) -> None:
        """Push back the byte most recently returned by :meth:`next_byte`."""

        self._pos -= 1

    async def skip_whitespace(self) -> None:
        while True:
            while self._pos < len(self._buffer):
                if self._buffer[self._pos] in _WS:
                    self._pos += 1
                else:
                    return
            if not await self._fill():
                return

    async def expect(self, byte: int) -> None:
        await self.skip_whitespace()
        got = await self.next_byte()
        if got == -1:
            raise InvalidImportPayloadError("unexpected end of the import body")
        if got != byte:
            raise InvalidImportPayloadError(
                f"malformed import body: expected {chr(byte)!r}"
            )

    async def try_consume(self, byte: int) -> bool:
        await self.skip_whitespace()
        got = await self.next_byte()
        if got == byte:
            return True
        if got != -1:
            self.unread()
        return False

    async def read_value(self) -> object:
        """Read one JSON scalar (string, number, true/false/null).

        Structural characters are not valid here; callers handle ``{`` and
        ``[`` themselves, so nested objects or arrays are rejected instead of
        being materialised.
        """

        await self.skip_whitespace()
        first = await self.next_byte()
        if first == -1:
            raise InvalidImportPayloadError("unexpected end of the import body")
        if first == _QUOTE:
            token = await self._read_string_token()
        else:
            token = await self._read_bare_token(first)
        try:
            return json.loads(token)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidImportPayloadError(
                "malformed JSON value in the import body"
            ) from exc

    async def _read_string_token(self) -> bytearray:
        """Read a JSON string, positioned just after its opening quote."""

        token = bytearray(b'"')
        while True:
            byte = await self.next_byte()
            if byte == -1:
                raise InvalidImportPayloadError(
                    "unterminated string in the import body"
                )
            token.append(byte)
            if byte == _BACKSLASH:
                escaped = await self.next_byte()
                if escaped == -1:
                    raise InvalidImportPayloadError(
                        "unterminated string in the import body"
                    )
                token.append(escaped)
            elif byte == _QUOTE:
                return token

    async def _read_bare_token(self, first: int) -> bytearray:
        """Read a number or literal token starting with ``first``."""

        token = bytearray()
        token.append(first)
        while True:
            byte = await self.next_byte()
            if byte == -1:
                return token
            if byte in _BARE_TOKEN_CHARS:
                token.append(byte)
            else:
                self.unread()
                return token


async def parse_import_payload(
    chunks: AsyncIterator[bytes],
    max_body_bytes: int = IMPORT_MAX_BODY_BYTES,
) -> tuple[str, dict[tuple[str, str], int], int]:
    """Stream-validate an import body and aggregate it into the fixed cells.

    Returns ``(week, counts, record_count)`` where ``counts`` maps
    ``(street, age_band)`` to its true count.  Raises
    :class:`DuplicatePatientError` when a de-identified patient number occurs
    twice in the batch and :class:`InvalidImportPayloadError` for any JSON or
    schema violation, including trailing data after the document.  Nothing is
    persisted here; the caller writes the aggregate in one transaction.
    """

    reader = _BodyReader(chunks, max_body_bytes)
    week: str | None = None
    counts: dict[tuple[str, str], int] | None = None
    record_count = 0

    await reader.expect(_OPEN_BRACE)
    if not await reader.try_consume(_CLOSE_BRACE):
        while True:
            key = await reader.read_value()
            if not isinstance(key, str):
                raise InvalidImportPayloadError(
                    "top-level field names must be strings"
                )
            await reader.expect(_COLON)
            if key == "week" and week is None:
                value = await reader.read_value()
                if not isinstance(value, str):
                    raise InvalidImportPayloadError("week must be a string")
                week = value
            elif key == "records" and counts is None:
                counts, record_count = await _read_records(reader)
            else:
                raise InvalidImportPayloadError(
                    "unexpected or duplicate top-level field in the import body"
                )
            await reader.skip_whitespace()
            separator = await reader.next_byte()
            if separator == _COMMA:
                continue
            if separator == _CLOSE_BRACE:
                break
            if separator == -1:
                raise InvalidImportPayloadError("unexpected end of the import body")
            raise InvalidImportPayloadError(
                "malformed import body: expected ',' or '}'"
            )

    await reader.skip_whitespace()
    if await reader.next_byte() != -1:
        raise InvalidImportPayloadError(
            "unexpected data after the end of the import body"
        )

    if week is None:
        raise InvalidImportPayloadError("week is required")
    try:
        validate_iso_week(week)
    except ValueError as exc:
        raise InvalidImportPayloadError(str(exc)) from exc
    if counts is None:
        raise InvalidImportPayloadError("records is required")
    if record_count == 0:
        raise InvalidImportPayloadError("records must contain at least one record")
    return week, counts, record_count


async def _read_records(
    reader: _BodyReader,
) -> tuple[dict[tuple[str, str], int], int]:
    """Stream the records array, de-duplicating and aggregating on the fly."""

    counts: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    record_count = 0

    await reader.expect(_OPEN_BRACKET)
    if await reader.try_consume(_CLOSE_BRACKET):
        return counts, 0
    while True:
        patient_id, street, age_band = await _read_record(reader)
        if patient_id in seen:
            # The identifier itself is never included in the error.
            raise DuplicatePatientError(
                "the same de-identified patient number appears more than once "
                "in this week; the entire batch was rejected"
            )
        seen.add(patient_id)
        cell = (street, age_band)
        counts[cell] = counts.get(cell, 0) + 1
        record_count += 1
        if record_count > IMPORT_MAX_RECORDS:
            raise InvalidImportPayloadError(
                f"records exceed the per-import limit of {IMPORT_MAX_RECORDS}"
            )
        await reader.skip_whitespace()
        separator = await reader.next_byte()
        if separator == _COMMA:
            continue
        if separator == _CLOSE_BRACKET:
            return counts, record_count
        if separator == -1:
            raise InvalidImportPayloadError("unexpected end of the import body")
        raise InvalidImportPayloadError(
            "malformed records array: expected ',' or ']'"
        )


async def _read_record(reader: _BodyReader) -> tuple[str, str, str]:
    """Read and validate one record object from the stream."""

    patient_id: str | None = None
    street: str | None = None
    age_band: str | None = None

    await reader.expect(_OPEN_BRACE)
    if not await reader.try_consume(_CLOSE_BRACE):
        while True:
            key = await reader.read_value()
            if not isinstance(key, str):
                raise InvalidImportPayloadError(
                    "record field names must be strings"
                )
            await reader.expect(_COLON)
            if key == "patient_id" and patient_id is None:
                patient_id = await _read_string_field(reader, "patient_id")
            elif key == "street" and street is None:
                street = await _read_string_field(reader, "street")
            elif key == "age_band" and age_band is None:
                age_band = await _read_string_field(reader, "age_band")
            else:
                raise InvalidImportPayloadError(
                    "unexpected or duplicate field in record"
                )
            await reader.skip_whitespace()
            separator = await reader.next_byte()
            if separator == _COMMA:
                continue
            if separator == _CLOSE_BRACE:
                break
            if separator == -1:
                raise InvalidImportPayloadError("unexpected end of the import body")
            raise InvalidImportPayloadError(
                "malformed record: expected ',' or '}'"
            )

    if patient_id is None or street is None or age_band is None:
        raise InvalidImportPayloadError(
            "each record needs patient_id, street and age_band"
        )
    patient_id = patient_id.strip()
    if not 1 <= len(patient_id) <= PATIENT_ID_MAX_LENGTH:
        raise InvalidImportPayloadError(
            f"patient_id must be 1-{PATIENT_ID_MAX_LENGTH} characters"
        )
    if street not in _STREET_SET:
        raise InvalidImportPayloadError(
            "unknown street; expected one of the fixed public groups: "
            + ", ".join(STREETS)
        )
    if age_band not in _AGE_BAND_SET:
        raise InvalidImportPayloadError(
            "unknown age_band; expected one of the fixed public groups: "
            + ", ".join(AGE_BANDS)
        )
    return patient_id, street, age_band


async def _read_string_field(reader: _BodyReader, name: str) -> str:
    value = await reader.read_value()
    if not isinstance(value, str):
        raise InvalidImportPayloadError(f"{name} must be a string")
    return value
