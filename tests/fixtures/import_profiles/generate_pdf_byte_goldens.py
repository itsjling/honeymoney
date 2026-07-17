"""Generate the committed, clean-room synthetic PDF import goldens.

The source layouts are the reviewed synthetic table/word fixtures beside each
output.  This generator intentionally uses only the Python standard library so
the PDF bytes do not depend on a PDF library version, clock, hostname, or
random identifier.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
PAGE_WIDTH = 612
PAGE_HEIGHT = 792


@dataclass(frozen=True)
class Fixture:
    profile_id: str
    source_name: str
    layout: str

    @property
    def case_dir(self) -> Path:
        return ROOT / self.profile_id / "accepted_statement"

    @property
    def output_path(self) -> Path:
        return self.case_dir / "input.pdf"


FIXTURES = (
    Fixture("hsbc_one_pdf", "words.json", "words"),
    Fixture("hsbc_hk_credit_card_pdf", "words.json", "words"),
    Fixture("mox_bank_pdf", "tables.json", "tables"),
    Fixture("mox_credit_card_pdf", "tables.json", "tables"),
)


def generated_fixtures() -> dict[str, bytes]:
    return {fixture.profile_id: _generate_fixture(fixture) for fixture in FIXTURES}


def _generate_fixture(fixture: Fixture) -> bytes:
    source = json.loads(
        (fixture.case_dir / fixture.source_name).read_text(encoding="utf-8")
    )
    pages = source["pages"]
    if fixture.layout == "words":
        streams = [_word_page(page) for page in pages]
    else:
        streams = [_table_page(page) for page in pages]
    return _pdf_document(streams)


def _word_page(words: list[dict[str, object]]) -> bytes:
    commands = [b"q"]
    for word in words:
        commands.append(
            _text_command(
                float(word["x0"]),
                float(word["top"]),
                str(word["text"]),
                size=4,
            )
        )
    commands.append(b"Q")
    return b"\n".join(commands) + b"\n"


def _table_page(tables: list[list[list[str | None]]]) -> bytes:
    commands = [b"q", b"0.5 w"]
    table_top = 48.0
    for table in tables:
        if not table:
            continue
        column_count = max(len(row) for row in table)
        left = 36.0
        right = 576.0
        column_width = (right - left) / column_count
        row_tops = [table_top]
        row_lines: list[list[list[str]]] = []
        for row in table:
            cells = [str(cell or "").splitlines() or [""] for cell in row]
            cells.extend([[""]] * (column_count - len(cells)))
            row_lines.append(cells)
            line_count = max(len(cell_lines) for cell_lines in cells)
            row_tops.append(row_tops[-1] + max(16.0, 9.0 * line_count + 6.0))

        for x in (left + column_width * index for index in range(column_count + 1)):
            commands.append(_line_command(x, row_tops[0], x, row_tops[-1]))
        for top in row_tops:
            commands.append(_line_command(left, top, right, top))

        for row_index, cells in enumerate(row_lines):
            for column_index, cell_lines in enumerate(cells):
                x = left + column_width * column_index + 4.0
                for line_index, text in enumerate(cell_lines):
                    commands.append(
                        _text_command(
                            x,
                            row_tops[row_index] + 4.0 + line_index * 9.0,
                            text,
                            size=7,
                        )
                    )
        table_top = row_tops[-1] + 24.0
    commands.append(b"Q")
    return b"\n".join(commands) + b"\n"


def _text_command(x: float, top: float, text: str, *, size: int) -> bytes:
    baseline = PAGE_HEIGHT - top - size
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return (
        f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {baseline:.2f} Tm ({escaped}) Tj ET"
    ).encode("ascii")


def _line_command(x1: float, top1: float, x2: float, top2: float) -> bytes:
    y1 = PAGE_HEIGHT - top1
    y2 = PAGE_HEIGHT - top2
    return f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S".encode("ascii")


def _pdf_document(page_streams: Iterable[bytes]) -> bytes:
    streams = list(page_streams)
    page_ids = [4 + index * 2 for index in range(len(streams))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Count "
            + str(len(streams)).encode("ascii")
            + b" /Kids ["
            + b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_ids)
            + b"] >>"
        ),
        3: (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        ),
    }
    for page_id, stream in zip(page_ids, streams):
        content_id = page_id + 1
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + f"{content_id} 0 R".encode("ascii")
            + b" >>"
        )
        objects[content_id] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        )

    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, max(objects) + 1):
        offsets.append(len(document))
        document.extend(f"{object_id} 0 obj\n".encode("ascii"))
        document.extend(objects[object_id])
        document.extend(b"\nendobj\n")

    xref_offset = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if a tracked PDF differs from deterministic generator output.",
    )
    args = parser.parse_args(argv)

    mismatches = []
    for fixture in FIXTURES:
        generated = _generate_fixture(fixture)
        if args.check:
            if not fixture.output_path.is_file():
                mismatches.append(f"missing {fixture.output_path.relative_to(ROOT)}")
            elif fixture.output_path.read_bytes() != generated:
                mismatches.append(f"stale {fixture.output_path.relative_to(ROOT)}")
        else:
            fixture.output_path.write_bytes(generated)
            print(f"wrote {fixture.output_path.relative_to(ROOT)}")
    if mismatches:
        for mismatch in mismatches:
            print(mismatch)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
