"""Resume upload: PDF validation, size limits, and workspace scoping.

The Gemini parse is stubbed — these cover the file handling around it, which
is where the security-relevant decisions live.
"""

import io
import uuid

import pytest
from fastapi.testclient import TestClient

import utils
from api.server import app

client = TestClient(app)


@pytest.fixture
def account():
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register", json={"email": email, "password": "password123"}
    )
    payload = response.json()
    return {
        "user_id": payload["user_id"],
        "headers": {"Authorization": f"Bearer {payload['token']}"},
    }


@pytest.fixture
def stub_gemini(monkeypatch):
    """Replace the AI call so tests never touch the network."""
    import api.server

    def fake_convert(raw_text):
        return {
            "name": "Test Candidate",
            "email": "test@example.com",
            "phone": "N/A",
            "location": "N/A",
            "linkedin": "N/A",
            "github": "N/A",
            "summary": raw_text[:50],
            "skills": ["python"],
            "experience": [],
            "education": [],
        }

    monkeypatch.setattr(api.server, "convert_pdf_to_json", fake_convert)


def make_pdf(text: str = "Jane Doe. Python engineer. FastAPI, SQL.") -> bytes:
    """A minimal single-page PDF whose text pypdf can extract."""
    escaped = text.replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()

    return bytes(out)


# =====================================================================
# THE FIXTURE ITSELF
# =====================================================================
def test_pdf_fixture_yields_extractable_text():
    """Guards the tests below: a broken fixture would make them meaningless."""
    text = utils.extract_pdf_text(io.BytesIO(make_pdf()))

    assert "Jane Doe" in text


# =====================================================================
# UPLOAD
# =====================================================================
def test_upload_creates_a_profile(account, stub_gemini):
    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", make_pdf(), "application/pdf")},
        data={"name": "AI Engineer"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "AI_Engineer.json"
    assert body["label"] == "Ai Engineer"

    listed = client.get("/profiles", headers=account["headers"]).json()["profiles"]
    assert [p["filename"] for p in listed] == ["AI_Engineer.json"]


def test_upload_requires_authentication():
    response = client.post(
        "/profiles/upload",
        files={"file": ("resume.pdf", make_pdf(), "application/pdf")},
        data={"name": "AI Engineer"},
    )

    assert response.status_code == 401


def test_upload_rejects_non_pdf(account, stub_gemini):
    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.docx", b"not a pdf", "application/msword")},
        data={"name": "Resume"},
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_upload_rejects_a_pdf_that_is_not_really_a_pdf(account, stub_gemini):
    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", b"totally not a pdf", "application/pdf")},
        data={"name": "Resume"},
    )

    assert response.status_code == 400


def test_upload_rejects_an_empty_file(account, stub_gemini):
    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", b"", "application/pdf")},
        data={"name": "Resume"},
    )

    assert response.status_code == 400


def test_upload_rejects_oversized_files(account, stub_gemini):
    oversized = make_pdf() + b"\x00" * (utils.MAX_RESUME_BYTES + 1)

    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", oversized, "application/pdf")},
        data={"name": "Resume"},
    )

    assert response.status_code == 413


def test_uploaded_name_is_sanitized(account, stub_gemini):
    """A traversal attempt in the profile name must not escape the workspace."""
    response = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", make_pdf(), "application/pdf")},
        data={"name": "../../../evil"},
    )

    assert response.status_code == 201
    assert ".." not in response.json()["filename"]


def test_uploads_are_isolated_between_accounts(account, stub_gemini):
    client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", make_pdf(), "application/pdf")},
        data={"name": "Mine"},
    )

    other = client.post(
        "/auth/register",
        json={"email": f"{uuid.uuid4().hex}@example.com", "password": "password123"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    assert client.get("/profiles", headers=other_headers).json()["profiles"] == []


# =====================================================================
# DELETE
# =====================================================================
def test_delete_profile(account, stub_gemini):
    created = client.post(
        "/profiles/upload",
        headers=account["headers"],
        files={"file": ("resume.pdf", make_pdf(), "application/pdf")},
        data={"name": "Temp"},
    ).json()

    response = client.delete(
        f"/profiles/{created['filename']}", headers=account["headers"]
    )

    assert response.status_code == 204
    assert client.get("/profiles", headers=account["headers"]).json()["profiles"] == []


def test_delete_missing_profile_is_404(account):
    response = client.delete("/profiles/nope.json", headers=account["headers"])

    assert response.status_code == 404


# =====================================================================
# EXTRACTION HELPER
# =====================================================================
def test_scanned_pdf_gives_a_clear_message():
    """An image-only PDF has no text layer; the message must say so."""
    blank = make_pdf(" ")

    with pytest.raises(utils.ResumeReadError, match="scanned image"):
        utils.extract_pdf_text(io.BytesIO(blank))


def test_corrupt_file_gives_a_clear_message():
    with pytest.raises(utils.ResumeReadError, match="could not be read"):
        utils.extract_pdf_text(io.BytesIO(b"garbage"))
