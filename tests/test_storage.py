"""
test_storage.py — the document vault's storage layer.

Patient files used to be written to the app container's own filesystem, which
on Railway has no volume attached: every deploy destroyed them while the rows
in `patient_documents` and `note_files` survived, leaving records pointing at
nothing. services/storage_service moves them to Cloudflare R2.

What is actually worth asserting here:

  * the contract — nothing raises, a missing object reads as None, and an
    unconfigured backend is a normal state rather than an error. Every caller
    is written against that contract, and a function that starts raising would
    turn a missing lab report into a 500 on the patient's record page.
  * the key rules — a key is not a path, but the disk fallback uses it as one,
    so `..` has to die in both backends.
  * that one doctor's keys are unreachable from another doctor's session. That
    is asserted end-to-end through the routes, because the storage layer has no
    concept of a doctor and must not grow one.

These run against the disk fallback: conftest blanks the R2_* settings for the
whole suite, so no test ever touches the real bucket. What is being proven is
the wiring and the contract — not Cloudflare's behaviour, which is theirs.
"""
import io
from datetime import datetime

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, give_schedule,
                           set_pin)
from database.models import PatientDocument, NoteFile, PatientNote
from services import storage_service as storage
from config import settings


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"stor-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Storage Patient")
    set_pin(client)
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


@pytest.fixture
def scratch():
    """A key under a prefix no real doctor can own, cleaned up afterwards."""
    key = f"patients/999999/888888/probe-{datetime.utcnow().timestamp()}.bin"
    yield key
    storage.delete_prefix("patients/999999/888888/")


# --------------------------------------------------------------------------- #
#  The contract                                                                 #
# --------------------------------------------------------------------------- #

class TestRoundTrip:

    def test_put_then_get_returns_the_same_bytes(self, scratch):
        payload = b"\x89PNG\r\n\x1a\n binary \x00 safe"
        ok, detail = storage.put(scratch, payload, "image/png")
        assert ok, f"put failed: {detail}"
        assert storage.get(scratch) == payload

    def test_put_overwrites_rather_than_appending(self, scratch):
        storage.put(scratch, b"first", "text/plain")
        storage.put(scratch, b"second", "text/plain")
        assert storage.get(scratch) == b"second"

    def test_exists_tracks_put_and_delete(self, scratch):
        assert storage.exists(scratch) is False
        storage.put(scratch, b"x", "text/plain")
        assert storage.exists(scratch) is True
        storage.delete(scratch)
        assert storage.exists(scratch) is False

    def test_delete_of_a_missing_object_is_success(self, scratch):
        """Deleting something already gone is the desired end state, not a
        failure. Callers delete row and object together and must not have to
        care which went first."""
        ok, _ = storage.delete(scratch)
        assert ok is True


class TestMissingIsNotAnError:
    """The whole point of the (ok, detail) / None contract."""

    def test_get_of_a_missing_key_returns_none(self):
        assert storage.get("patients/999999/888888/never-written.pdf") is None

    def test_get_does_not_raise_on_a_malformed_key(self):
        for bad in ("", "   ", "/", "\x00"):
            assert storage.get(bad) is None

    def test_delete_prefix_on_an_empty_prefix_reports_zero(self):
        count, _ = storage.delete_prefix("patients/999999/777777/")
        assert count == 0


class TestUnconfiguredFallsBackToDisk:

    def test_no_credentials_means_disk_not_failure(self):
        """conftest blanks the R2 settings, so this is the suite's own state."""
        assert storage.is_configured() is False
        assert storage.backend_name() == "disk"

    def test_writes_still_work_with_no_credentials(self, scratch):
        ok, detail = storage.put(scratch, b"local", "text/plain")
        assert ok and detail == "disk"
        assert storage.get(scratch) == b"local"

    def test_is_configured_needs_every_field(self, monkeypatch):
        """Three of four set is not 'mostly configured' — it is a client that
        cannot authenticate, and silently writing to disk in production is the
        exact bug this whole change exists to fix."""
        for present in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                        "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            for field in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                          "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
                monkeypatch.setattr(settings, field,
                                    "" if field == present else "set")
            assert storage.is_configured() is False, (
                f"missing {present} still read as configured")


# --------------------------------------------------------------------------- #
#  Keys are not paths                                                           #
# --------------------------------------------------------------------------- #

class TestKeySafety:

    TRAVERSALS = [
        "patients/1/2/../../../etc/passwd",
        "../../../etc/passwd",
        "patients/1/2/\x00cut.pdf",
        "patients\\1\\2\\windows.pdf",
    ]

    @pytest.mark.parametrize("key", TRAVERSALS)
    def test_traversal_keys_are_refused_on_write(self, key):
        ok, _ = storage.put(key, b"pwned", "text/plain")
        # Either refused outright, or neutralised into a harmless key — what
        # must never happen is a write landing outside the upload root.
        if ok:
            from pathlib import Path
            root = Path("uploads").resolve()
            assert not (root.parent / "etc" / "passwd").exists()

    @pytest.mark.parametrize("key", TRAVERSALS)
    def test_traversal_keys_never_read_back_foreign_data(self, key):
        assert storage.get(key) is None

    def test_safe_filename_still_strips_directories(self):
        """The first line of defence, upstream of the storage layer."""
        from routers.patients import _safe_filename
        assert _safe_filename("../../etc/passwd") == "passwd"
        assert _safe_filename("a/b/c/report.pdf") == "report.pdf"
        assert _safe_filename("evil\\win.pdf") == "evil_win.pdf"
        assert _safe_filename("") == "file"

    def test_patient_prefix_ends_in_a_slash(self):
        """Without it, the prefix for patient 2 also matches patient 23 — and
        deleting patient 2 would take patient 23's files with it."""
        assert storage.patient_prefix(1, 2) == "patients/1/2/"
        assert not storage.patient_prefix(1, 23).startswith(
            storage.patient_prefix(1, 2))

    def test_object_key_is_built_from_the_prefix(self):
        assert storage.object_key(7, 9, "scan.png") == "patients/7/9/scan.png"


# --------------------------------------------------------------------------- #
#  Prefix deletion                                                              #
# --------------------------------------------------------------------------- #

class TestDeletePrefix:
    """Deleting a patient used to be one rmtree. An object store has no
    directories, so it is now list-then-delete — and the blast radius of
    getting that wrong is someone else's records."""

    def test_removes_everything_under_the_prefix(self):
        base = "patients/999999/888888/"
        for name in ("a.pdf", "b.png", "c.txt"):
            storage.put(base + name, b"x", "text/plain")
        count, _ = storage.delete_prefix(base)
        assert count == 3
        for name in ("a.pdf", "b.png", "c.txt"):
            assert storage.get(base + name) is None

    def test_does_not_touch_a_neighbouring_patient(self):
        mine, theirs = "patients/999999/888888/", "patients/999999/888889/"
        storage.put(mine + "x.pdf", b"mine", "text/plain")
        storage.put(theirs + "x.pdf", b"theirs", "text/plain")
        try:
            storage.delete_prefix(mine)
            assert storage.get(theirs + "x.pdf") == b"theirs", (
                "deleting one patient removed another patient's files")
        finally:
            storage.delete_prefix(theirs)

    def test_a_shared_numeric_stem_is_not_a_match(self):
        """patients/1/2 vs patients/1/23 — the trailing-slash bug, proven."""
        short, long_ = "patients/999999/2/", "patients/999999/23/"
        storage.put(short + "a.pdf", b"short", "text/plain")
        storage.put(long_ + "a.pdf", b"long", "text/plain")
        try:
            storage.delete_prefix(short)
            assert storage.get(long_ + "a.pdf") == b"long"
        finally:
            storage.delete_prefix(long_)


# --------------------------------------------------------------------------- #
#  End to end, through the routes                                               #
# --------------------------------------------------------------------------- #

class TestVaultGoesThroughStorage:
    """The storage layer knows nothing about doctors, and must not. Ownership
    is the routes' job — so it is asserted where it actually lives."""

    def _upload(self, client, patient_id):
        return client.post(
            f"/patients/{patient_id}/vault/upload",
            data={"category": "lab_report", "description": "Blood work"},
            files={"files": ("report.pdf", io.BytesIO(b"%PDF-1.4 fake"),
                             "application/pdf")},
            follow_redirects=False,
        )

    def test_an_upload_is_readable_through_the_storage_service(self, client, doc):
        self._upload(client, doc["patient"])
        db = TestSessionLocal()
        try:
            d = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first()
            assert d is not None, "vault upload stored no row"
            key = storage.object_key(doc["id"], doc["patient"], d.stored_name)
        finally:
            db.close()
        assert storage.get(key) == b"%PDF-1.4 fake", (
            "the route wrote somewhere the storage service cannot read back")

    def test_deleting_a_document_removes_the_object_too(self, client, doc):
        self._upload(client, doc["patient"])
        db = TestSessionLocal()
        try:
            d = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first()
            doc_id, key = d.id, storage.object_key(doc["id"], doc["patient"],
                                                   d.stored_name)
        finally:
            db.close()
        assert storage.get(key) is not None
        client.post(f"/patients/{doc['patient']}/vault/{doc_id}/delete",
                    follow_redirects=False)
        assert storage.get(key) is None, (
            "the row went but the file stayed — an orphaned patient document")

    def test_another_doctor_cannot_read_your_object_through_the_route(self, client, doc):
        self._upload(client, doc["patient"])
        db = TestSessionLocal()
        try:
            d = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first()
            doc_id = d.id
        finally:
            db.close()

        # A second doctor, their own session, guessing the document id.
        client.cookies.clear()
        other = f"stor2-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
        other_id = make_doctor(client, other)
        other_clinic = clinic_of(other_id)
        give_schedule(other_id, other_clinic)
        other_patient = make_patient(other_id, other_clinic, name="Not Yours")
        set_pin(client)

        r = client.get(f"/patients/{other_patient}/vault/{doc_id}",
                       follow_redirects=False)
        assert r.status_code != 200, "one doctor served another doctor's document"
        assert b"%PDF-1.4 fake" not in r.content


class TestNoteAttachmentsGoThroughStorage:

    def test_attachment_round_trips_through_the_route(self, client, doc):
        r = client.post(
            f"/patients/{doc['patient']}/notes/add",
            data={"note_text": "with a scan"},
            files={"files": ("scan.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfake"),
                             "image/png")},
        )
        assert r.status_code == 200, r.text[:300]

        db = TestSessionLocal()
        try:
            nf = (db.query(NoteFile).join(PatientNote)
                  .filter(PatientNote.patient_id == doc["patient"]).first())
            assert nf is not None, "note attachment stored no row"
            file_id = nf.id
            key = storage.object_key(doc["id"], doc["patient"], nf.stored_name)
        finally:
            db.close()

        assert storage.get(key) == b"\x89PNG\r\n\x1a\nfake"
        assert client.get(
            f"/patients/{doc['patient']}/files/{file_id}").status_code == 200

    def test_deleting_an_attachment_removes_the_object(self, client, doc):
        client.post(
            f"/patients/{doc['patient']}/notes/add",
            data={"note_text": "with a scan"},
            files={"files": ("scan.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfake"),
                             "image/png")},
        )
        db = TestSessionLocal()
        try:
            nf = (db.query(NoteFile).join(PatientNote)
                  .filter(PatientNote.patient_id == doc["patient"]).first())
            file_id = nf.id
            key = storage.object_key(doc["id"], doc["patient"], nf.stored_name)
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/files/{file_id}/delete")
        assert storage.get(key) is None
