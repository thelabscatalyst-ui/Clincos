"""
test_vault_trash.py — deleting a vault document is recoverable for 30 days.

R2 has no object versioning (ListObjectVersions answers NotImplemented), so
nothing at the bucket level can undo a delete. Without this, a doctor who
removes the wrong lab report loses it outright.

Delete now moves the file under trash/ in the same bucket and stamps
`deleted_at` on the row. The vault hides it, "Recently deleted" lists it,
Restore brings it back, and a daily job purges anything older than the window.

What matters most here, in order:

  * nothing a doctor deletes is destroyed on the spot;
  * a trashed document cannot be downloaded — "deleted" has to mean deleted to
    anyone holding an old link;
  * one doctor can never restore or purge another doctor's document;
  * restore never brings back a row whose file is gone, which would put a
    document in the vault that can never be opened;
  * the purge removes what is old and nothing that is not.

All of this runs on the disk fallback — conftest blanks R2_* for the suite.
"""
import io
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, give_schedule,
                           set_pin)
from database.models import PatientDocument
from services import storage_service as storage
from services.scheduler_service import purge_vault_trash
from config import settings


SCRATCH = "patients/999999/888888/"


@pytest.fixture(autouse=True)
def _clean_scratch():
    yield
    storage.delete_prefix(SCRATCH)
    storage.delete_prefix(storage.TRASH_PREFIX + SCRATCH)
    storage.delete_prefix(storage.TRASH_PREFIX + "patients/999999/888889/")


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"trash-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Trash Patient")
    set_pin(client)
    return {"id": did, "clinic": cid, "patient": pid}


def _upload(client, patient_id, name="report.pdf", body=b"%PDF-1.4 trash test"):
    client.post(f"/patients/{patient_id}/vault/upload",
                data={"category": "lab_report", "description": "x"},
                files={"files": (name, io.BytesIO(body), "application/pdf")},
                follow_redirects=False)
    db = TestSessionLocal()
    try:
        d = (db.query(PatientDocument)
             .filter(PatientDocument.patient_id == patient_id,
                     PatientDocument.original_name == name)
             .order_by(PatientDocument.id.desc()).first())
        return d.id, d.stored_name
    finally:
        db.close()


def _row(doc_id):
    db = TestSessionLocal()
    try:
        return db.query(PatientDocument).filter(PatientDocument.id == doc_id).first()
    finally:
        db.close()


def _set_deleted_at(doc_id, when):
    db = TestSessionLocal()
    try:
        db.query(PatientDocument).filter(PatientDocument.id == doc_id) \
          .update({"deleted_at": when})
        db.commit()
    finally:
        db.close()


def _age_on_disk(key, days):
    """Backdate a trashed object, the way 30 days of waiting would."""
    p = Path("uploads") / key
    old = time.time() - days * 86400
    os.utime(p, (old, old))


# --------------------------------------------------------------------------- #
#  Storage primitives                                                           #
# --------------------------------------------------------------------------- #

class TestTrashPrimitives:

    def test_trash_moves_the_object_rather_than_deleting_it(self):
        key = SCRATCH + "a.pdf"
        storage.put(key, b"lab report", "application/pdf")
        ok, _ = storage.trash(key)
        assert ok
        assert storage.get(key) is None, "still live after trashing"
        assert storage.get(storage.trash_key(key)) == b"lab report", (
            "the bytes were destroyed instead of moved to the trash")

    def test_restore_moves_it_back(self):
        key = SCRATCH + "b.pdf"
        storage.put(key, b"xray", "application/pdf")
        storage.trash(key)
        ok, _ = storage.restore(key)
        assert ok
        assert storage.get(key) == b"xray"
        assert storage.get(storage.trash_key(key)) is None

    def test_trashing_an_already_lost_file_is_not_an_error(self):
        """Files destroyed by pre-R2 deploys still have rows; the doctor must
        still be able to clear them out of the vault."""
        assert storage.trash(SCRATCH + "never-existed.pdf") == (True, "missing")

    def test_restoring_a_purged_file_fails(self):
        ok, detail = storage.restore(SCRATCH + "gone.pdf")
        assert ok is False and detail == "missing"

    def test_trash_restarts_the_clock(self):
        """A rename keeps the upload's mtime. Without refreshing it, a file
        uploaded 40 days ago would be purged the moment it was trashed."""
        key = SCRATCH + "old-upload.pdf"
        storage.put(key, b"old", "application/pdf")
        _age_on_disk(key, 40)
        storage.trash(key)
        removed, _ = storage.purge_trash()
        assert removed == 0
        assert storage.get(storage.trash_key(key)) == b"old"

    def test_purge_removes_only_what_has_expired(self):
        old, new = SCRATCH + "old.pdf", SCRATCH + "new.pdf"
        for k in (old, new):
            storage.put(k, b"x", "application/pdf")
            storage.trash(k)
        _age_on_disk(storage.trash_key(old), 31)
        _age_on_disk(storage.trash_key(new), 29)

        removed, _ = storage.purge_trash()
        assert removed == 1
        assert storage.get(storage.trash_key(old)) is None
        assert storage.get(storage.trash_key(new)) == b"x", (
            "purged a file still inside the retention window")

    def test_purge_never_touches_live_files(self):
        key = SCRATCH + "live.pdf"
        storage.put(key, b"live", "application/pdf")
        _age_on_disk(key, 400)
        storage.purge_trash()
        assert storage.get(key) == b"live"

    def test_trash_prefix_takes_one_patient_and_not_the_next(self):
        mine, theirs = SCRATCH, "patients/999999/888889/"
        storage.put(mine + "a.pdf", b"a", "application/pdf")
        storage.put(mine + "b.pdf", b"b", "application/pdf")
        storage.put(theirs + "c.pdf", b"c", "application/pdf")
        try:
            moved, _ = storage.trash_prefix(mine)
            assert moved == 2
            assert storage.get(storage.trash_key(mine + "a.pdf")) == b"a"
            assert storage.get(theirs + "c.pdf") == b"c", (
                "deleting one patient trashed another patient's files")
        finally:
            storage.delete_prefix(theirs)


class TestTrashWithABrokenVault:
    """Same rule as the rest of storage: configured-but-unreachable never
    quietly uses the container disk."""

    @pytest.fixture
    def broken_r2(self, monkeypatch):
        for f in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                  "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            monkeypatch.setattr(settings, f, "configured-but-broken")
        monkeypatch.setattr(storage, "_r2", lambda: None)

    def test_trash_fails_loudly(self, broken_r2):
        ok, detail = storage.trash(SCRATCH + "x.pdf")
        assert ok is False and detail == "vault unavailable"

    def test_restore_fails_loudly(self, broken_r2):
        ok, detail = storage.restore(SCRATCH + "x.pdf")
        assert ok is False and detail == "vault unavailable"

    def test_purge_removes_nothing(self, broken_r2):
        assert storage.purge_trash() == (0, "vault unavailable")


# --------------------------------------------------------------------------- #
#  Routes                                                                       #
# --------------------------------------------------------------------------- #

class TestDeleteMovesToTrash:

    def test_delete_keeps_the_row_and_moves_the_file(self, client, doc):
        did, stored = _upload(client, doc["patient"])
        key = storage.object_key(doc["id"], doc["patient"], stored)

        client.post(f"/patients/{doc['patient']}/vault/{did}/delete",
                    follow_redirects=False)

        row = _row(did)
        assert row is not None and row.deleted_at is not None
        assert storage.get(key) is None
        assert storage.get(storage.trash_key(key)) == b"%PDF-1.4 trash test"

    def test_a_trashed_document_cannot_be_downloaded(self, client, doc):
        did, _ = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        r = client.get(f"/patients/{doc['patient']}/vault/{did}",
                       follow_redirects=False)
        assert r.status_code != 200
        assert b"%PDF-1.4 trash test" not in r.content

    def test_still_blocked_when_the_original_was_left_behind(self, client, doc):
        """The row filter is the guard, not the empty path.

        On R2 a move is copy-then-delete. If the copy succeeds and the delete
        of the original fails, the file exists in BOTH places while the row is
        marked deleted. The test above passes even with vault_serve's
        deleted_at filter removed — the download is blocked only because the
        live key happens to be empty. This recreates the partial failure so
        the filter itself is what is under test (verified by mutation).
        """
        did, stored = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        storage.put(storage.object_key(doc["id"], doc["patient"], stored),
                    b"%PDF-1.4 trash test", "application/pdf")

        r = client.get(f"/patients/{doc['patient']}/vault/{did}",
                       follow_redirects=False)
        assert r.status_code != 200, (
            "a deleted document was served because its original lingered")

    def test_it_leaves_the_vault_and_appears_under_recently_deleted(self, client, doc):
        did, _ = _upload(client, doc["patient"], name="vanishing.pdf")
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        page = client.get(f"/patients/{doc['patient']}/vault").text

        assert "Recently deleted" in page
        assert f"/vault/{did}/restore" in page
        # Not listed as a live document: no download link for it.
        assert f"/vault/{did}?download=true" not in page

    def test_the_patient_badge_stops_counting_it(self, client, doc):
        """The vault badge on the patient page reads doc_count. Starlette's
        TestClient exposes the template context, so assert on the number the
        page was given rather than scraping the rendered badge."""
        did, _ = _upload(client, doc["patient"])
        before = client.get(f"/patients/{doc['patient']}")
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        after = client.get(f"/patients/{doc['patient']}")
        assert before.context["doc_count"] == 1
        assert after.context["doc_count"] == 0, (
            "the trashed document is still counted on the patient page")

    def test_the_confirmation_no_longer_says_permanently(self, client, doc):
        _upload(client, doc["patient"])
        page = client.get(f"/patients/{doc['patient']}/vault").text
        assert "removed permanently" not in page
        assert "Recently deleted" in page or "restore it" in page

    def test_deleting_twice_is_harmless(self, client, doc):
        did, _ = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        first = _row(did).deleted_at
        r = client.post(f"/patients/{doc['patient']}/vault/{did}/delete",
                        follow_redirects=False)
        assert r.status_code in (302, 303)
        assert _row(did).deleted_at == first, "a second delete restarted the clock"

    def test_a_trashed_document_cannot_be_edited(self, client, doc):
        did, _ = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        client.post(f"/patients/{doc['patient']}/vault/{did}/edit",
                    data={"category": "xray_scan", "description": "sneaky"})
        assert _row(did).description != "sneaky"


class TestRestore:

    def test_restore_brings_it_back_and_it_downloads(self, client, doc):
        did, stored = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        client.post(f"/patients/{doc['patient']}/vault/{did}/restore")

        assert _row(did).deleted_at is None
        r = client.get(f"/patients/{doc['patient']}/vault/{did}")
        assert r.status_code == 200 and r.content == b"%PDF-1.4 trash test"

    def test_restore_of_a_lost_file_leaves_it_in_the_trash(self, client, doc):
        """Restoring a row with no file would put back a document that can
        never be opened. It stays trashed and the page says why."""
        did, stored = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        storage.delete(storage.trash_key(
            storage.object_key(doc["id"], doc["patient"], stored)))

        r = client.post(f"/patients/{doc['patient']}/vault/{did}/restore",
                        follow_redirects=False)
        assert "restore_failed=1" in r.headers.get("location", "")
        assert _row(did).deleted_at is not None

        page = client.get(f"/patients/{doc['patient']}/vault?restore_failed=1").text
        assert "couldn't be restored" in page

    def test_restore_ignores_a_live_document(self, client, doc):
        did, _ = _upload(client, doc["patient"])
        r = client.post(f"/patients/{doc['patient']}/vault/{did}/restore",
                        follow_redirects=False)
        assert r.status_code in (302, 303)
        assert _row(did).deleted_at is None


class TestDeleteForever:

    def test_purge_removes_row_and_file(self, client, doc):
        did, stored = _upload(client, doc["patient"])
        key = storage.object_key(doc["id"], doc["patient"], stored)
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        client.post(f"/patients/{doc['patient']}/vault/{did}/purge")

        assert _row(did) is None
        assert storage.get(storage.trash_key(key)) is None

    def test_a_live_document_cannot_be_purged_in_one_step(self, client, doc):
        """Delete forever exists only inside the trash, so no single click can
        destroy a file outright."""
        did, stored = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/purge")
        assert _row(did) is not None and _row(did).deleted_at is None
        assert storage.get(storage.object_key(doc["id"], doc["patient"], stored)) is not None


class TestAnotherDoctorsTrash:
    """Restore and purge are as sensitive as download."""

    def _second_doctor(self, client):
        client.cookies.clear()
        email = f"trash2-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
        other = make_doctor(client, email)
        oc = clinic_of(other)
        give_schedule(other, oc)
        op = make_patient(other, oc, name="Someone Else")
        set_pin(client)
        return op

    def test_cannot_restore_your_document(self, client, doc):
        did, _ = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        their_patient = self._second_doctor(client)

        for pid in (their_patient, doc["patient"]):
            r = client.post(f"/patients/{pid}/vault/{did}/restore",
                            follow_redirects=False)
            # The document must be INVISIBLE to them, not found-then-failed.
            # Without the ownership filter the route finds the row, builds the
            # object key from the requester's id, misses, and redirects with
            # restore_failed — so the row stays trashed and the assertion below
            # would pass by accident. Checking the redirect is what makes this
            # test fail when the filter is gone (verified by mutation).
            assert "restore_failed" not in r.headers.get("location", ""), (
                "another doctor's restore reached your document")
        assert _row(did).deleted_at is not None, "another doctor restored your document"

    def test_cannot_purge_your_document(self, client, doc):
        did, stored = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        their_patient = self._second_doctor(client)

        for pid in (their_patient, doc["patient"]):
            client.post(f"/patients/{pid}/vault/{did}/purge")
        assert _row(did) is not None, "another doctor erased your document"
        assert storage.get(storage.trash_key(
            storage.object_key(doc["id"], doc["patient"], stored))) is not None

    def test_does_not_see_it_under_recently_deleted(self, client, doc):
        did, _ = _upload(client, doc["patient"], name="private-scan.pdf")
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        their_patient = self._second_doctor(client)
        page = client.get(f"/patients/{their_patient}/vault").text
        assert "private-scan.pdf" not in page


# --------------------------------------------------------------------------- #
#  The daily purge job                                                          #
# --------------------------------------------------------------------------- #

class TestDailyPurge:

    def test_expired_documents_go_and_recent_ones_stay(self, client, doc):
        old_id, old_stored = _upload(client, doc["patient"], name="old.pdf")
        new_id, new_stored = _upload(client, doc["patient"], name="new.pdf")
        for did in (old_id, new_id):
            client.post(f"/patients/{doc['patient']}/vault/{did}/delete")

        now = datetime.utcnow()
        _set_deleted_at(old_id, now - timedelta(days=31))
        _set_deleted_at(new_id, now - timedelta(days=29))

        db = TestSessionLocal()
        try:
            result = purge_vault_trash(now=now, db=db)
        finally:
            db.close()

        assert result["rows"] >= 1
        assert _row(old_id) is None
        assert storage.get(storage.trash_key(
            storage.object_key(doc["id"], doc["patient"], old_stored))) is None
        assert _row(new_id) is not None, "purged a document still inside the window"
        assert storage.get(storage.trash_key(
            storage.object_key(doc["id"], doc["patient"], new_stored))) is not None

    def test_a_row_is_kept_if_its_file_could_not_be_removed(self, client, doc,
                                                            monkeypatch):
        """Deleting the row anyway would orphan a file nobody can see or
        remove. It waits for tomorrow's run instead."""
        did, _ = _upload(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/vault/{did}/delete")
        _set_deleted_at(did, datetime.utcnow() - timedelta(days=45))
        monkeypatch.setattr(storage, "delete", lambda key: (False, "r2 error"))

        db = TestSessionLocal()
        try:
            purge_vault_trash(db=db)
        finally:
            db.close()
        assert _row(did) is not None

    def test_never_touches_live_documents(self, client, doc):
        did, stored = _upload(client, doc["patient"])
        db = TestSessionLocal()
        try:
            purge_vault_trash(now=datetime.utcnow() + timedelta(days=400), db=db)
        finally:
            db.close()
        assert _row(did) is not None and _row(did).deleted_at is None
        assert storage.get(storage.object_key(doc["id"], doc["patient"], stored)) is not None

    def test_the_job_is_scheduled(self):
        """A purge that is never scheduled looks exactly like one that works,
        until the trash fills up."""
        # Read the file, not the function: conftest replaces start_scheduler
        # with a mock so the suite never runs real background jobs.
        src = (Path(__file__).resolve().parent.parent
               / "services" / "scheduler_service.py").read_text()
        start = src[src.index("def start_scheduler"):src.index("def stop_scheduler")]
        assert "purge_vault_trash" in start and 'id="vault_trash_purge"' in start
