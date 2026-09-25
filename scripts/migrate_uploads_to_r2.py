"""
migrate_uploads_to_r2.py — move any surviving local files into the vault, and
report the ones that are already gone.

Background: patient files used to be written to the app container's filesystem,
which on Railway has no volume. Every deploy destroyed them while the rows in
`patient_documents` and `note_files` survived. This script does two jobs:

  1. Upload whatever is still on disk to R2, under the same key layout.
  2. Say which database rows point at an object that does not exist. Those are
     files already lost to past deploys. The count is a damage report — worth
     knowing now rather than discovering through a doctor asking where their
     lab report went.

    python scripts/migrate_uploads_to_r2.py            # report only, uploads nothing
    python scripts/migrate_uploads_to_r2.py --apply    # actually upload

Reporting is the default on purpose: a migration that runs the moment you type
its name gives you nowhere to stand if it is pointed at the wrong bucket.
"""
import argparse
import mimetypes
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings                                    # noqa: E402
from database.connection import SessionLocal                   # noqa: E402
from database.models import PatientDocument, NoteFile, PatientNote  # noqa: E402
from services import storage_service as storage                # noqa: E402

LOCAL_ROOT = Path("uploads")


def _rows(db):
    """Every (doctor_id, patient_id, stored_name, label) the database expects.

    NoteFile has no doctor_id of its own — it hangs off PatientNote, which has
    both. Joining is what makes the key reconstructable.
    """
    for d in db.query(PatientDocument).all():
        yield d.doctor_id, d.patient_id, d.stored_name, f"document #{d.id} ({d.original_name})"
    for nf, note in (db.query(NoteFile, PatientNote)
                     .join(PatientNote, NoteFile.note_id == PatientNote.id).all()):
        yield note.doctor_id, note.patient_id, nf.stored_name, f"attachment #{nf.id} ({nf.original_name})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually upload; without it nothing is written")
    args = ap.parse_args()

    print(f"\nbackend      : {storage.backend_name()}")
    print(f"bucket       : {settings.R2_BUCKET or '(none — disk fallback)'}")
    print(f"local source : {LOCAL_ROOT.resolve()}")
    if not args.apply:
        print("mode         : REPORT ONLY (pass --apply to upload)")

    if not storage.is_configured():
        print("\nR2 is not configured in this environment, so there is nothing to")
        print("migrate to — the app is already reading and writing these same")
        print("files through the disk fallback. Set the R2_* variables first.\n")
        return 1

    # ---- 1. upload whatever is still on disk --------------------------------
    local_files = sorted(p for p in LOCAL_ROOT.rglob("*") if p.is_file()) \
        if LOCAL_ROOT.exists() else []
    print(f"\n=== Local files: {len(local_files)} ===")

    uploaded = skipped = failed = 0
    for path in local_files:
        key = str(path.relative_to(LOCAL_ROOT))
        if storage.exists(key):
            skipped += 1
            continue
        if not args.apply:
            print(f"  would upload  {key}")
            uploaded += 1
            continue
        mime, _ = mimetypes.guess_type(path.name)
        ok, detail = storage.put(key, path.read_bytes(),
                                 mime or "application/octet-stream")
        if ok:
            uploaded += 1
            print(f"  uploaded      {key}")
        else:
            failed += 1
            print(f"  FAILED        {key} — {detail}")

    print(f"\n  uploaded {uploaded}   already present {skipped}   failed {failed}")

    # ---- 2. report rows whose object is missing ------------------------------
    db = SessionLocal()
    try:
        expected = list(_rows(db))
    finally:
        db.close()

    missing = []
    for doctor_id, patient_id, stored_name, label in expected:
        if not storage.exists(storage.object_key(doctor_id, patient_id, stored_name)):
            missing.append((doctor_id, patient_id, label))

    print(f"\n=== Database rows: {len(expected)} ===")
    if not missing:
        print("  every row has its file. Nothing was lost.\n")
        return 0

    print(f"  {len(missing)} row(s) point at a file that no longer exists.")
    print("  These were destroyed by earlier deploys and cannot be recovered")
    print("  from here — the bytes are gone. Listed so the affected doctors can")
    print("  be told, rather than finding out when they open the record:\n")
    by_doctor: dict[int, list[str]] = {}
    for doctor_id, patient_id, label in missing:
        by_doctor.setdefault(doctor_id, []).append(f"patient {patient_id}: {label}")
    for doctor_id, items in sorted(by_doctor.items()):
        print(f"  doctor {doctor_id} — {len(items)} file(s)")
        for item in items[:10]:
            print(f"      {item}")
        if len(items) > 10:
            print(f"      … and {len(items) - 10} more")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
