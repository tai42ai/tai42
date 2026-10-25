"""Stored-object bytes for the entry-door suites, built in the test, and the refusal texts they answer."""

from __future__ import annotations

import io
import struct
import zlib

SQL = "SELECT id, name FROM accounts WHERE id = {{ account_id }} ORDER BY name LIMIT 10;\n"
SQL_LATIN1 = "SELECT id, name FROM accounts WHERE name = 'café' ORDER BY name;\n".encode("latin-1")
JAVASCRIPT = (
    "function greet(name) {\n  const message = `Hello ${name}`;\n  console.log(message);\n  return message;\n}\n"
)
RAGGED_CSV = "id,name,score\n1,alice,3\n2,bob,4,extra\n3,carol,5\n4,dan,6\n"


def locked_pdf_message(resource_id: str) -> str:
    return f"{resource_id!r} could not be extracted as pdf: FileNotDecryptedError: File has not been decrypted"


def ragged_csv_message(resource_id: str) -> str:
    return (
        f"{resource_id!r} could not be extracted as csv: "
        "ParserError: Error tokenizing data. C error: Expected 3 fields in line 3, saw 4"
    )


def not_loadable_prefix(resource_id: str) -> str:
    """The head of the ``400`` an entry door answers for content it refuses to load."""
    return f"resource {resource_id!r} cannot be loaded: "


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
