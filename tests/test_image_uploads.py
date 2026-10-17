"""
test_image_uploads.py — every uploaded image is stored as a JPEG.

The first real production upload was an iPhone photo in HEIC: stored
perfectly, viewable by nobody, because Chrome, Firefox and Edge cannot render
HEIC. Images are now re-encoded to JPEG on the way in; documents are not.

Fixtures are generated in code (pillow-heif can write HEIC), so nothing binary
is checked into the repo.
"""
import io
from datetime import datetime

import pytest
from PIL import Image

pillow_heif = pytest.importorskip("pillow_heif")
pillow_heif.register_heif_opener()

from tests.conftest import TestSessionLocal
from tests.helpers import make_doctor, clinic_of, make_patient, give_schedule, set_pin
from database.models import PatientDocument, NoteFile, PatientNote
from services import storage_service as storage
from services.image_service import prepare_upload, MAX_EDGE

JPEG_MAGIC = b"\xff\xd8\xff"


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    b = io.BytesIO()
    img.save(b, fmt, **kw)
    return b.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# --------------------------------------------------------------------------- #
#  Conversion rules                                                             #
# --------------------------------------------------------------------------- #

class TestImagesBecomeJpeg:

    @pytest.mark.parametrize("name,fmt", [
        ("IMG_0001.HEIC", "HEIF"),
        ("scan.png", "PNG"),
        ("photo.webp", "WEBP"),
        ("anim.gif", "GIF"),
        ("old-scan.tiff", "TIFF"),
        ("legacy.bmp", "BMP"),
        ("already.jpg", "JPEG"),
    ])
    def test_every_image_format_comes_out_as_jpeg(self, name, fmt):
        src = _encode(Image.new("RGB", (120, 80), (30, 120, 200)), fmt)
        p = prepare_upload(name, src)
        assert p is not None, f"{name} was rejected"
        assert p.converted is True
        assert p.mime == "image/jpeg"
        assert p.data[:3] == JPEG_MAGIC, f"{name} was not re-encoded"
        assert p.name.endswith(".jpg")
        assert _open(p.data).size == (120, 80)

    def test_the_name_keeps_its_stem(self):
        p = prepare_upload("CBC Report 12 Oct.HEIC",
                           _encode(Image.new("RGB", (10, 10)), "HEIF"))
        assert p.name == "CBC Report 12 Oct.jpg"

    def test_uppercase_and_heif_extensions_are_recognised(self):
        for name in ("A.HEIC", "b.heif", "c.Heic"):
            assert prepare_upload(name, _encode(Image.new("RGB", (8, 8)), "HEIF")).converted


class TestDocumentsAreUntouched:

    @pytest.mark.parametrize("name,mime", [
        ("lab-report.pdf", "application/pdf"),
        ("discharge.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("notes.txt", "text/plain"),
    ])
    def test_non_images_pass_through_byte_for_byte(self, name, mime):
        data = b"%PDF-1.4 multi page report" if name.endswith(".pdf") else b"some bytes"
        p = prepare_upload(name, data)
        assert p.converted is False
        assert p.data == data, "a document was altered"
        assert p.name == name
        assert p.mime == mime


class TestWhatTheConversionFixes:

    def test_phone_rotation_is_applied(self):
        """iPhones store portrait shots landscape plus an 'orientation' flag.
        Re-encoding without applying it would save them sideways."""
        img = Image.new("RGB", (200, 100), (0, 0, 0))
        exif = img.getexif()
        exif[0x0112] = 6                      # Orientation: rotate 90° CW
        src = _encode(img, "JPEG", exif=exif.tobytes())

        out = _open(prepare_upload("portrait.jpg", src).data)
        assert out.size == (100, 200), "portrait photo stored sideways"

    def test_gps_and_all_exif_are_stripped(self):
        img = Image.new("RGB", (50, 50))
        exif = img.getexif()
        exif[0x010F] = "Apple"                 # Make
        exif[0x8825] = {1: "N", 2: (12.0, 58.0, 0.0)}  # GPS IFD
        src = _encode(img, "JPEG", exif=exif.tobytes())
        assert _open(src).getexif(), "fixture should carry EXIF"

        out = _open(prepare_upload("geo.jpg", src).data)
        assert dict(out.getexif()) == {}, "metadata (incl. location) survived"

    def test_transparency_is_flattened_onto_white(self):
        """JPEG has no alpha; a transparent PNG must not come out black."""
        src = _encode(Image.new("RGBA", (20, 20), (0, 0, 0, 0)), "PNG")
        out = _open(prepare_upload("logo.png", src).data).convert("RGB")
        assert out.getpixel((0, 0)) == (255, 255, 255)

    def test_16_bit_scans_keep_their_contrast(self):
        """A plain convert clips 16-bit values above 255 — the whole scan
        would come out white."""
        img = Image.new("I;16", (64, 64))
        img.putdata([i * 1000 for i in range(64)] * 64)   # 0 … 63000
        src = _encode(img, "PNG")
        out = _open(prepare_upload("xray.png", src).data).convert("L")
        lo, hi = out.getextrema()
        assert hi - lo > 200, f"16-bit scan lost its range: {lo}..{hi}"

    def test_cmyk_comes_out_rgb(self):
        src = _encode(Image.new("CMYK", (16, 16), (0, 255, 255, 0)), "JPEG")
        assert _open(prepare_upload("print.jpg", src).data).mode == "RGB"

    def test_huge_photos_are_capped(self):
        src = _encode(Image.new("RGB", (MAX_EDGE * 2, 1000)), "PNG")
        out = _open(prepare_upload("panorama.png", src).data)
        assert max(out.size) == MAX_EDGE
        assert out.size[1] == 500, "aspect ratio not preserved"


class TestUnreadableImagesAreRefused:

    def test_corrupt_bytes_with_an_image_name(self):
        assert prepare_upload("broken.jpg", b"not really a jpeg") is None

    def test_corrupt_heic(self):
        assert prepare_upload("broken.HEIC", b"\x00\x00\x00\x18ftypheic garbage") is None

    def test_svg_is_refused_not_served(self):
        """SVG is image/* but carries script. Served inline, it would run in
        the app's origin. Pillow cannot rasterise it, so it is refused."""
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        assert prepare_upload("evil.svg", svg) is None


# --------------------------------------------------------------------------- #
#  Through the routes                                                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"img-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Image Patient")
    set_pin(client)
    return {"id": did, "patient": pid}


def _latest_doc(patient_id):
    db = TestSessionLocal()
    try:
        return (db.query(PatientDocument)
                .filter(PatientDocument.patient_id == patient_id)
                .order_by(PatientDocument.id.desc()).first())
    finally:
        db.close()


class TestVaultStoresJpeg:

    def _upload(self, client, pid, name, data, mime="application/octet-stream"):
        return client.post(f"/patients/{pid}/vault/upload",
                           data={"category": "lab_report", "description": ""},
                           files={"files": (name, io.BytesIO(data), mime)},
                           follow_redirects=False)

    def test_an_iphone_photo_is_stored_and_served_as_jpeg(self, client, doc):
        heic = _encode(Image.new("RGB", (300, 400), (180, 60, 60)), "HEIF")
        self._upload(client, doc["patient"], "IMG_4512.HEIC", heic, "image/heic")

        d = _latest_doc(doc["patient"])
        assert d.original_name == "IMG_4512.jpg"
        assert d.mime_type == "image/jpeg"
        assert d.stored_name.endswith(".jpg")
        stored = storage.get(storage.object_key(doc["id"], doc["patient"], d.stored_name))
        assert stored[:3] == JPEG_MAGIC and d.file_size == len(stored)

        r = client.get(f"/patients/{doc['patient']}/vault/{d.id}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["content-disposition"].startswith("inline"), (
            "a JPEG should preview in the browser, not force a download")

    def test_a_pdf_is_stored_untouched(self, client, doc):
        pdf = b"%PDF-1.4\n2 pages of lab results\n%%EOF"
        self._upload(client, doc["patient"], "results.pdf", pdf, "application/pdf")
        d = _latest_doc(doc["patient"])
        assert d.original_name == "results.pdf" and d.mime_type == "application/pdf"
        assert storage.get(storage.object_key(doc["id"], doc["patient"], d.stored_name)) == pdf

    def test_an_unreadable_image_is_refused_and_the_doctor_is_told(self, client, doc):
        r = self._upload(client, doc["patient"], "broken.HEIC", b"garbage", "image/heic")
        assert _latest_doc(doc["patient"]) is None, "stored a file nobody can open"
        assert "unreadable=1" in r.headers["location"]
        page = client.get(r.headers["location"]).text
        assert "couldn't be uploaded" in page

    def test_one_bad_file_does_not_sink_the_good_ones(self, client, doc):
        good = _encode(Image.new("RGB", (40, 40)), "PNG")
        r = client.post(
            f"/patients/{doc['patient']}/vault/upload",
            data={"category": "lab_report", "description": ""},
            files=[("files", ("good.png", io.BytesIO(good), "image/png")),
                   ("files", ("bad.jpg", io.BytesIO(b"nope"), "image/jpeg"))],
            follow_redirects=False)
        d = _latest_doc(doc["patient"])
        assert d is not None and d.original_name == "good.jpg"
        assert "unreadable=1" in r.headers["location"]

    def test_garbage_in_the_query_string_is_harmless(self, client, doc):
        for v in ("abc", "-5", "999999", ""):
            assert client.get(f"/patients/{doc['patient']}/vault?unreadable={v}"
                              ).status_code == 200


class TestNoteAttachmentsStoreJpeg:

    def test_heic_attachment_becomes_jpeg(self, client, doc):
        heic = _encode(Image.new("RGB", (60, 80), (10, 200, 10)), "HEIF")
        r = client.post(f"/patients/{doc['patient']}/notes/add",
                        data={"note_text": "photo of wound"},
                        files={"files": ("IMG_9.HEIC", io.BytesIO(heic), "image/heic")})
        assert r.status_code == 200

        db = TestSessionLocal()
        try:
            nf = (db.query(NoteFile).join(PatientNote)
                  .filter(PatientNote.patient_id == doc["patient"]).first())
            assert nf.original_name == "IMG_9.jpg"
            fid = nf.id
        finally:
            db.close()

        v = client.get(f"/patients/{doc['patient']}/files/{fid}")
        assert v.status_code == 200
        assert v.headers["content-type"] == "image/jpeg"
        assert v.content[:3] == JPEG_MAGIC
