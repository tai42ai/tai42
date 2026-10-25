"""Stored-object bytes for the content classifier and the entry doors, built in the test."""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib

SQL = b"SELECT id, name FROM accounts WHERE id = {{ account_id }} ORDER BY name LIMIT 10;\n"
SQL_LATIN1 = "SELECT id, name FROM accounts WHERE name = 'café' ORDER BY name;\n".encode("latin-1")
JAVASCRIPT = (
    b"function greet(name) {\n  const message = `Hello ${name}`;\n  console.log(message);\n  return message;\n}\n"
)
RAGGED_CSV = b"id,name,score\n1,alice,3\n2,bob,4,extra\n3,carol,5\n4,dan,6\n"
CSV = b"id,name,score\n1,alice,3\n2,bob,4\n"
HTML = b"<!DOCTYPE html><html><head><title>Notes</title></head><body><h1>Notes</h1><p>Plain page.</p></body></html>\n"
TEMPLATED_HTML = b"<!DOCTYPE html><html><head><title>Hi</title></head><body><p>Hello {{ name }}</p></body></html>\n"

LOCKED_PDF_MESSAGE = (
    "'docs/locked.pdf' could not be extracted as pdf: FileNotDecryptedError: File has not been decrypted"
)
RAGGED_CSV_MESSAGE = (
    "'data/scores.csv' could not be extracted as csv: "
    "ParserError: Error tokenizing data. C error: Expected 3 fields in line 3, saw 4"
)
FILES_INSTALL_LINE = (
    "tai42-skeleton 'files' resource loading requires the 'files' optional "
    "dependencies (magika, pandas, langchain-community, pypdf, docx2txt, "
    "openpyxl, tabulate, beautifulsoup4, python-pptx, striprtf, odfpy, "
    "ebooklib). Install them with: pip install 'tai42-skeleton[files]'"
)


def pdf_with_text(text: str) -> bytes:
    """A one-page PDF whose content stream shows ``text`` in Helvetica."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


NOTES_PDF = pdf_with_text("Quarterly notes")


def locked_pdf() -> bytes:
    """A one-page blank PDF encrypted with a user password."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def png(width: int = 4, height: int = 4) -> bytes:
    """An uncompressed RGB PNG of ``width`` x ``height`` pixels."""
    raw = b"".join(b"\x00" + bytes((x * 7 + y * 13) % 256 for x in range(width * 3)) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 0))
        + chunk(b"IEND", b"")
    )


def zip_archive() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("a.txt", "hello " * 50)
    return out.getvalue()


def large_json() -> bytes:
    return json.dumps({f"k{i}": list(range(i % 20)) for i in range(400)}).encode()


def large_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in range(1, 400):
        sheet.append([f"row {row}", row, row * 1.5, f"value {row * 7}"])
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
