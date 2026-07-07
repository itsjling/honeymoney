"""Generate privacy-safe synthetic PDFs for manual parser experiments.

The committed JSON fixtures in this directory are the canonical automated test
fixtures. This script is optional and requires the `pdf` extra so maintainers can
produce simple text-based PDFs when tuning pdfplumber/PyMuPDF behavior locally.
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    try:
        import fitz
    except ImportError:
        print("Install PDF extras first: python3 -m pip install '.[pdf]'")
        return 1

    fixture_dir = Path(__file__).resolve().parent
    output_dir = fixture_dir / "generated"
    output_dir.mkdir(exist_ok=True)

    for fixture_path in sorted(fixture_dir.glob("*.json")):
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        document = fitz.open()
        for page_tables in data.get("pages", []):
            page = document.new_page()
            y = 72
            for table in page_tables:
                for row in table:
                    cells = row if isinstance(row, list) else [row]
                    page.insert_text((72, y), "    ".join(str(cell) for cell in cells))
                    y += 14
                y += 14
        document.save(output_dir / f"{fixture_path.stem}.pdf")
    print(f"Wrote synthetic PDFs to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
