"""Streaming, schema-tolerant parse of USPTO patent-assignment XML.

Files can be very large (bulk daily/annual dumps), so parsing **streams** with
``lxml.iterparse`` and clears processed elements to keep memory flat regardless of file size.
Extraction is schema-tolerant: missing/renamed tags yield ``None`` rather than raising, so
minor DTD-version differences do not crash the run.
"""

from __future__ import annotations

import zipfile
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, Final

from lxml import etree

from .model import Assignee, Assignment, Assignor, ExtractedRecord, PropertyRow

# lxml exposes its element type only under the underscore-prefixed ``_Element``; alias it once
# (suppressing the one privacy warning) and annotate with the public-looking alias everywhere.
type XmlElement = etree._Element  # pyright: ignore[reportPrivateUsage]

# The element that delimits one logical record in the stream.
ASSIGNMENT_TAG: Final = "patent-assignment"
# Child element of assignment-record holding correspondent (agent) contact details.
_CORRESPONDENT: Final = "correspondent"


# --------------------------------------------------------------------------------------
# Schema-tolerant extraction helpers
# --------------------------------------------------------------------------------------
def _clean(value: str | None) -> str | None:
    """Collapse surrounding whitespace; return None for missing/blank text."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _first_text(el: XmlElement, *paths: str) -> str | None:
    """Return the first non-empty text found among ``paths`` (relative to ``el``)."""
    for path in paths:
        text = el.findtext(path)
        cleaned = _clean(text)
        if cleaned is not None:
            return cleaned
    return None


def _full_text(el: XmlElement, path: str) -> str | None:
    """Return the concatenated text of a child element, flattening inline markup.

    USPTO ``invention-title`` may contain formatting tags (``<b>``, ``<i>``, ``<sup>`` …);
    ``findtext`` would stop at the first such child, so we join all descendant text instead
    and collapse internal whitespace.

    Args:
        el: The element to search under.
        path: Relative path to the child whose full text is wanted.

    Returns:
        The flattened text, or None if the child is absent/empty.
    """
    child = el.find(path)
    if child is None:
        return None
    parts = [t.strip() for t in child.itertext() if isinstance(t, str) and t.strip()]
    joined = " ".join(parts)
    return joined or None


def _date(el: XmlElement | None, *containers: str) -> str | None:
    """Extract a date, handling both ``<x>YYYYMMDD</x>`` and ``<x><date>YYYYMMDD</date></x>``.

    Args:
        el: The element to search under.
        containers: Candidate container tag names holding the date.

    Returns:
        The date text, or None if absent.
    """
    if el is None:
        return None
    for container in containers:
        child = el.find(container)
        if child is None:
            continue
        # Prefer a nested <date>; fall back to the container's own text.
        nested = _clean(child.findtext("date"))
        if nested is not None:
            return nested
        own = _clean(child.text)
        if own is not None:
            return own
    return None


def _extract_assignment(record: XmlElement) -> Assignment:
    corr = record.find(_CORRESPONDENT)
    return Assignment(
        reel_no=_first_text(record, "reel-no"),
        frame_no=_first_text(record, "frame-no"),
        last_update_date=_date(record, "last-update-date"),
        recorded_date=_date(record, "recorded-date"),
        purge_indicator=_first_text(record, "purge-indicator"),
        page_count=_first_text(record, "page-count"),
        conveyance_text=_first_text(record, "conveyance-text"),
        correspondent_name=_first_text(corr, "name") if corr is not None else None,
        correspondent_address_1=_first_text(corr, "address-1") if corr is not None else None,
        correspondent_address_2=_first_text(corr, "address-2") if corr is not None else None,
        correspondent_address_3=_first_text(corr, "address-3") if corr is not None else None,
        correspondent_address_4=_first_text(corr, "address-4") if corr is not None else None,
    )


def _extract_assignors(
    assignment: XmlElement, reel_no: str | None, frame_no: str | None
) -> list[Assignor]:
    rows: list[Assignor] = []
    for a in assignment.iterfind("patent-assignors/patent-assignor"):
        rows.append(
            Assignor(
                reel_no=reel_no,
                frame_no=frame_no,
                name=_first_text(a, "name"),
                execution_date=_date(a, "execution-date"),
                date_acknowledged=_date(a, "date-acknowledged"),
            )
        )
    return rows


def _extract_assignees(
    assignment: XmlElement, reel_no: str | None, frame_no: str | None
) -> list[Assignee]:
    rows: list[Assignee] = []
    for a in assignment.iterfind("patent-assignees/patent-assignee"):
        rows.append(
            Assignee(
                reel_no=reel_no,
                frame_no=frame_no,
                name=_first_text(a, "name"),
                address_1=_first_text(a, "address-1"),
                address_2=_first_text(a, "address-2"),
                city=_first_text(a, "city"),
                state=_first_text(a, "state"),
                country_name=_first_text(a, "country-name", "country"),
                postcode=_first_text(a, "postcode"),
            )
        )
    return rows


def _extract_properties(
    assignment: XmlElement, reel_no: str | None, frame_no: str | None
) -> list[PropertyRow]:
    rows: list[PropertyRow] = []
    for prop in assignment.iterfind("patent-properties/patent-property"):
        # invention-title may embed inline markup (<b>, <sup>, ...), so read its full text.
        title = _full_text(prop, "invention-title")
        doc_ids = prop.findall("document-id")
        if not doc_ids:
            # A property with no document-id still deserves a row (title-only).
            rows.append(
                PropertyRow(
                    reel_no=reel_no,
                    frame_no=frame_no,
                    invention_title=title,
                    doc_country=None,
                    doc_number=None,
                    doc_kind=None,
                    doc_name=None,
                    doc_date=None,
                )
            )
            continue
        for doc in doc_ids:
            rows.append(
                PropertyRow(
                    reel_no=reel_no,
                    frame_no=frame_no,
                    invention_title=title,
                    doc_country=_first_text(doc, "country"),
                    doc_number=_first_text(doc, "doc-number"),
                    doc_kind=_first_text(doc, "kind"),
                    doc_name=_first_text(doc, "name"),
                    doc_date=_date(doc, "date") or _first_text(doc, "date"),
                )
            )
    return rows


def extract(assignment: XmlElement) -> ExtractedRecord:
    """Extract all normalized rows from one ``patent-assignment`` element.

    The reel/frame key is read from the ``assignment-record`` header (falling back to the
    assignment element itself) and propagated to every child row so the tables can be joined.
    """
    record = assignment.find("assignment-record")
    header_el = record if record is not None else assignment
    header = _extract_assignment(header_el)
    return ExtractedRecord(
        assignment=header,
        assignors=_extract_assignors(assignment, header.reel_no, header.frame_no),
        assignees=_extract_assignees(assignment, header.reel_no, header.frame_no),
        properties=_extract_properties(assignment, header.reel_no, header.frame_no),
    )


# --------------------------------------------------------------------------------------
# Streaming parse
# --------------------------------------------------------------------------------------
def _stream_elements(source: Any) -> Iterator[XmlElement]:
    """Yield each ``patent-assignment`` element from an XML source, clearing as it goes.

    ``source`` is anything ``lxml.etree.iterparse`` accepts — a filename or a binary file
    object. After each element is yielded, it and its already-parsed previous siblings are
    removed so peak memory stays flat regardless of input size. ``recover=True`` lets parsing
    continue past minor malformations common in bulk dumps.
    """
    context = etree.iterparse(source, events=("end",), tag=ASSIGNMENT_TAG, recover=True)
    for _event, elem in context:
        yield elem
        # Free this element and any preceding siblings that lxml still holds.
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            previous = elem.getprevious()
            while previous is not None:
                parent.remove(previous)
                previous = elem.getprevious()
    del context


def iter_assignments(path: Path) -> Iterator[XmlElement]:
    """Yield each ``patent-assignment`` element from ``path``, streaming with bounded memory.

    Accepts either a raw ``.xml`` file or a ``.zip`` as downloaded from USPTO (the XML is read
    straight from the archive without extracting it to disk).

    Args:
        path: Path to a USPTO assignment ``.xml`` or ``.zip`` file.

    Yields:
        Each ``patent-assignment`` element in document order.

    Raises:
        ValueError: If ``path`` is a zip with no ``.xml`` member.
    """
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".xml")]
            if not members:
                raise ValueError(f"{path} contains no .xml member")
            with archive.open(members[0]) as source:
                yield from _stream_elements(source)
    else:
        # Open the file ourselves (rather than pass a filename to lxml) so it closes
        # deterministically — even when the caller stops early — instead of at GC time.
        with path.open("rb") as source:
            yield from _stream_elements(source)


def iter_records(path: Path) -> Generator[ExtractedRecord, None, None]:
    """Stream ``ExtractedRecord``s from an XML/ZIP file (parse + extract combined).

    Returns a generator (closeable) so callers can stop early and release the underlying file.
    """
    for assignment in iter_assignments(path):
        yield extract(assignment)
