import math
import mimetypes
import uuid
from datetime import date
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, Request, Depends, Form, Query, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

PER_PAGE = 10

from datetime import datetime

from database.connection import get_db
from database.models import Doctor, Patient, Appointment, AppointmentStatus, PatientNote, NoteFile, PinnedPatient, Bill, PatientDocument, DOCUMENT_CATEGORIES, ReferralSource, Prescription
from services.auth_service import get_paying_doctor, require_pin

router = APIRouter(prefix="/patients", tags=["patients"])
templates = Jinja2Templates(directory="templates")

MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB

# MIME types that browsers render inline without XSS risk.
# Deliberately excludes text/html and application/javascript.
_INLINE_MIME_PREFIXES = ("image/",)
_INLINE_MIME_EXACT    = {"application/pdf", "text/plain", "text/csv"}


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _ordinal(n: int) -> str:
    """Return ordinal string: 1 → '1st', 23 → '23rd', etc."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    elif n % 10 == 1:
        suffix = "st"
    elif n % 10 == 2:
        suffix = "nd"
    elif n % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return f"{n}{suffix}"


def _date_label(dt) -> str:
    """Format a datetime as '23rd April, 2026'."""
    from datetime import datetime as _dt
    if not dt:
        return ""
    if isinstance(dt, str):
        dt = _dt.fromisoformat(dt)
    return f"{_ordinal(dt.day)} {dt.strftime('%B, %Y')}"


def _fmt_size(b: int | None) -> str:
    if not b:
        return ""
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024*1024):.1f} MB"


def _notes_data(patient_notes) -> list:
    """Convert PatientNote ORM objects → plain dicts for templates / JSON."""
    out = []
    for n in patient_notes:
        out.append({
            "id":         n.id,
            "text":       n.note_text,
            "date_label": _date_label(n.created_at),
            "files": [
                {
                    "id":   f.id,
                    "name": f.original_name,
                    "size": _fmt_size(f.file_size),
                }
                for f in (n.files or [])
            ],
        })
    return out


def _upload_dir(doctor_id: int, patient_id: int) -> Path:
    p = Path(f"uploads/patients/{doctor_id}/{patient_id}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(original: str) -> str:
    """Strip any directory components so callers cannot traverse outside uploads/.
    e.g. '../../etc/passwd' → 'etc_passwd', 'report.pdf' → 'report.pdf'
    """
    # Path.name strips everything before the last separator
    name = Path(original).name
    # Replace any remaining separators (Windows backslash, null bytes, etc.)
    for ch in ("\x00", "/", "\\", ":"):
        name = name.replace(ch, "_")
    return name or "file"


# --------------------------------------------------------------------------- #
#  List                                                                         #
# --------------------------------------------------------------------------- #

@router.get("", response_class=HTMLResponse)
def patients_list(
    request: Request,
    q: str = Query(default=""),
    sort: str = Query(default="last_seen"),
    page: int = Query(default=1),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    # ── Pinned patients (ordered oldest→newest so index 0 = first pinned) ──
    pins = db.query(PinnedPatient).filter(
        PinnedPatient.doctor_id == doctor.id
    ).order_by(PinnedPatient.pinned_at.asc()).all()
    pinned_ids   = [p.patient_id for p in pins]   # oldest first
    pinned_set   = set(pinned_ids)

    # ── Name of oldest pin (shown in JS confirm when adding a 4th) ──────
    oldest_pin_name = None
    if len(pinned_ids) >= 3:
        oldest_pin_patient = db.query(Patient).filter(
            Patient.id == pinned_ids[0],
            Patient.doctor_id == doctor.id,
        ).first()
        oldest_pin_name = oldest_pin_patient.name if oldest_pin_patient else None

    # ── Total patients (all, for header display) ─────────────────────────
    total = db.query(func.count(Patient.id)).filter(
        Patient.doctor_id == doctor.id
    ).scalar()

    # ── Pinned patients (always fully shown, filtered by search if active) ─
    pinned_base = db.query(Patient).filter(
        Patient.doctor_id == doctor.id,
        Patient.id.in_(pinned_ids) if pinned_ids else False,
    )
    if q.strip():
        term = f"%{q.strip()}%"
        pinned_base = pinned_base.filter(or_(Patient.name.ilike(term), Patient.phone.ilike(term)))
    pinned_all = pinned_base.all()
    pinned_map  = {p.id: p for p in pinned_all}
    pinned_list = [pinned_map[pid] for pid in pinned_ids if pid in pinned_map]

    # ── Other patients — paginated ────────────────────────────────────────
    other_base = db.query(Patient).filter(Patient.doctor_id == doctor.id)
    if pinned_ids:
        other_base = other_base.filter(~Patient.id.in_(pinned_ids))
    if q.strip():
        term = f"%{q.strip()}%"
        other_base = other_base.filter(or_(Patient.name.ilike(term), Patient.phone.ilike(term)))

    total_other = other_base.with_entities(func.count(Patient.id)).scalar()

    total_pages = max(1, math.ceil(total_other / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    if sort == "alpha":
        other_list = other_base.order_by(Patient.name.asc()).offset(offset).limit(PER_PAGE).all()
    else:
        other_list = other_base.order_by(
            Patient.last_visit.desc(), Patient.created_at.desc()
        ).offset(offset).limit(PER_PAGE).all()

    page_start = offset + 1 if total_other > 0 else 0
    page_end   = min(offset + PER_PAGE, total_other)

    return templates.TemplateResponse(request, "patients.html", {
        "doctor":           doctor,
        "pinned_patients":  pinned_list,
        "other_patients":   other_list,
        "pinned_ids":       pinned_set,
        "oldest_pin_name":  oldest_pin_name,
        "pin_count":        len(pinned_ids),
        "total":            total,
        "total_other":      total_other,
        "page":             page,
        "total_pages":      total_pages,
        "page_start":       page_start,
        "page_end":         page_end,
        "per_page":         PER_PAGE,
        "q":                q,
        "sort":             sort,
        "active":           "patients",
    })


# --------------------------------------------------------------------------- #
#  Pin / Unpin                                                                  #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/pin", response_class=HTMLResponse)
def pin_patient(
    patient_id: int,
    q: str = Form(default=""),
    sort: str = Form(default="last_seen"),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    # Already pinned → no-op
    existing = db.query(PinnedPatient).filter(
        PinnedPatient.doctor_id == doctor.id,
        PinnedPatient.patient_id == patient_id,
    ).first()
    if not existing:
        # If already at 3, remove the oldest (FIFO)
        pin_count = db.query(func.count(PinnedPatient.id)).filter(
            PinnedPatient.doctor_id == doctor.id
        ).scalar()
        if pin_count >= 3:
            oldest = db.query(PinnedPatient).filter(
                PinnedPatient.doctor_id == doctor.id
            ).order_by(PinnedPatient.pinned_at.asc()).first()
            if oldest:
                db.delete(oldest)

        db.add(PinnedPatient(
            doctor_id=doctor.id,
            patient_id=patient_id,
            pinned_at=datetime.utcnow(),
        ))
        db.commit()

    back = f"/patients?sort={sort}" + (f"&q={q}" if q.strip() else "")
    return RedirectResponse(url=back, status_code=303)


@router.post("/{patient_id}/unpin", response_class=HTMLResponse)
def unpin_patient(
    patient_id: int,
    q: str = Form(default=""),
    sort: str = Form(default="last_seen"),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    db.query(PinnedPatient).filter(
        PinnedPatient.doctor_id == doctor.id,
        PinnedPatient.patient_id == patient_id,
    ).delete()
    db.commit()

    back = f"/patients?sort={sort}" + (f"&q={q}" if q.strip() else "")
    return RedirectResponse(url=back, status_code=303)


# --------------------------------------------------------------------------- #
#  Edit referral source                                                         #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/source")
def update_patient_source(
    patient_id: int,
    referral_source: str = Form(""),
    referral_source_other: str = Form(""),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    """Set/edit the patient's marketing referral source from the profile page.

    Unlike the auto-capture in booking flows (which preserves the first touch),
    an explicit edit from the profile *does* override any previous value — that
    is the entire purpose of this endpoint.
    """
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    raw = referral_source.strip()
    if not raw:
        # Clearing the source
        patient.referral_source = None
        patient.referral_source_other = None
    else:
        try:
            patient.referral_source = ReferralSource(raw)
            if raw == ReferralSource.other.value:
                patient.referral_source_other = referral_source_other.strip()[:120] or None
            else:
                patient.referral_source_other = None
        except ValueError:
            # Unknown enum value — silently no-op so we don't 500
            pass

    db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)


# --------------------------------------------------------------------------- #
#  Detail                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/{patient_id}", response_class=HTMLResponse)
def patient_detail(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    # ── One-time migration: move legacy patient.notes → PatientNote row ──
    if patient.notes:
        existing_count = db.query(func.count(PatientNote.id)).filter(
            PatientNote.patient_id == patient.id
        ).scalar()
        if existing_count == 0:
            legacy = PatientNote(
                patient_id=patient.id,
                doctor_id=doctor.id,
                note_text=patient.notes,
                created_at=patient.created_at,   # approximate original date
            )
            db.add(legacy)
            patient.notes = None
            db.commit()

    appointments = (
        db.query(Appointment)
        .filter(
            Appointment.patient_id == patient.id,
            Appointment.doctor_id  == doctor.id,
        )
        .order_by(
            Appointment.appointment_date.desc(),
            Appointment.appointment_time.desc(),
        )
        .all()
    )

    raw_notes = (
        db.query(PatientNote)
        .filter(PatientNote.patient_id == patient.id, PatientNote.doctor_id == doctor.id)
        .order_by(PatientNote.created_at.desc())
        .all()
    )

    completed = sum(1 for a in appointments if a.status == AppointmentStatus.completed)
    upcoming  = sum(1 for a in appointments if a.status == AppointmentStatus.scheduled
                    and a.appointment_date >= date.today())

    bills = (
        db.query(Bill)
        .filter(Bill.patient_id == patient.id, Bill.doctor_id == doctor.id)
        .order_by(Bill.created_at.desc())
        .all()
    )

    doc_count = db.query(func.count(PatientDocument.id)).filter(
        PatientDocument.patient_id == patient.id,
        PatientDocument.doctor_id  == doctor.id,
    ).scalar() or 0

    rx_count = db.query(func.count(Prescription.id)).filter(
        Prescription.patient_id == patient.id,
        Prescription.doctor_id  == doctor.id,
    ).scalar() or 0

    recent_prescriptions = (
        db.query(Prescription)
        .filter(
            Prescription.patient_id == patient.id,
            Prescription.doctor_id  == doctor.id,
        )
        .order_by(Prescription.created_at.desc())
        .limit(3)
        .all()
    )

    return templates.TemplateResponse(request, "patient_detail.html", {
        "doctor":               doctor,
        "patient":              patient,
        "appointments":         appointments,
        "notes_data":           _notes_data(raw_notes),
        "completed":            completed,
        "upcoming":             upcoming,
        "bills":                bills,
        "doc_count":            doc_count,
        "rx_count":             rx_count,
        "recent_prescriptions": recent_prescriptions,
        "active":               "patients",
        "pin_required": getattr(request.state, "pin_required", False),
    })


# --------------------------------------------------------------------------- #
#  Add Note (AJAX — multipart: text + optional files)                          #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes/add")
async def add_note(
    patient_id: int,
    note_text: str = Form(""),
    files: List[UploadFile] = File(default=[]),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    text = note_text.strip()
    real_files = [f for f in files if f.filename]

    if not text and not real_files:
        return JSONResponse({"error": "Note cannot be empty."}, status_code=400)

    # Create the note row
    note = PatientNote(
        patient_id=patient_id,
        doctor_id=doctor.id,
        note_text=text or "(files attached)",
    )
    db.add(note)
    db.flush()   # populate note.id before inserting files

    saved_files = []
    udir = _upload_dir(doctor.id, patient_id)

    for f in real_files:
        content = await f.read()
        if len(content) > MAX_FILE_BYTES:
            continue   # silently skip oversized files

        stored_name = f"{uuid.uuid4().hex}_{_safe_filename(f.filename)}"
        dest = udir / stored_name
        async with aiofiles.open(dest, "wb") as fh:
            await fh.write(content)

        nf = NoteFile(
            note_id=note.id,
            original_name=f.filename,
            stored_name=stored_name,
            file_size=len(content),
        )
        db.add(nf)
        db.flush()
        saved_files.append({
            "id":   nf.id,
            "name": f.filename,
            "size": _fmt_size(len(content)),
        })

    db.commit()

    return JSONResponse({
        "note_id":    note.id,
        "date_label": _date_label(note.created_at),
        "text":       note.note_text,
        "files":      saved_files,
    })


# --------------------------------------------------------------------------- #
#  Download File                                                                #
# --------------------------------------------------------------------------- #

@router.get("/{patient_id}/files/{file_id}")
def view_file(
    patient_id: int,
    file_id: int,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    nf = db.query(NoteFile).join(PatientNote).filter(
        NoteFile.id == file_id,
        PatientNote.doctor_id == doctor.id,
        PatientNote.patient_id == patient_id,
    ).first()
    if not nf:
        return JSONResponse({"error": "File not found."}, status_code=404)

    path = Path(f"uploads/patients/{doctor.id}/{patient_id}/{nf.stored_name}")
    if not path.exists():
        return JSONResponse({"error": "File missing on disk."}, status_code=404)

    mime_type, _ = mimetypes.guess_type(nf.original_name)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Only serve inline for safe previewable types.
    # text/html and application/javascript are intentionally excluded to
    # prevent stored-XSS from a doctor uploading a crafted HTML file.
    previewable = (
        any(mime_type.startswith(p) for p in _INLINE_MIME_PREFIXES) or
        mime_type in _INLINE_MIME_EXACT
    )
    disposition = "inline" if previewable else "attachment"

    safe_name = nf.original_name.replace('"', '').replace("'", '')
    return FileResponse(
        path=str(path),
        media_type=mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
    )


# --------------------------------------------------------------------------- #
#  Delete Note                                                                  #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes/{note_id}/delete")
def delete_note(
    patient_id: int,
    note_id: int,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    note = db.query(PatientNote).filter(
        PatientNote.id == note_id,
        PatientNote.patient_id == patient_id,
        PatientNote.doctor_id == doctor.id,
    ).first()
    if note:
        # Delete physical files from disk
        udir = Path(f"uploads/patients/{doctor.id}/{patient_id}")
        for nf in note.files:
            p = udir / nf.stored_name
            if p.exists():
                p.unlink()
        db.delete(note)
        db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
#  Edit Note (AJAX — update text + add more files)                             #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes/{note_id}/edit")
async def edit_note(
    patient_id: int,
    note_id: int,
    note_text: str = Form(""),
    new_files: List[UploadFile] = File(default=[]),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    note = db.query(PatientNote).filter(
        PatientNote.id == note_id,
        PatientNote.patient_id == patient_id,
        PatientNote.doctor_id == doctor.id,
    ).first()
    if not note:
        return JSONResponse({"error": "Note not found."}, status_code=404)

    text = note_text.strip()
    real_files = [f for f in new_files if f.filename]

    # Must have at least some content after edit
    remaining_files = len(note.files)
    if not text and remaining_files == 0 and not real_files:
        return JSONResponse({"error": "Note cannot be empty."}, status_code=400)

    # Update text: use submitted value, or fall back to "(files attached)" when blanked
    note.note_text = text if text else "(files attached)"

    # Save any new files
    if real_files:
        udir = _upload_dir(doctor.id, patient_id)
        for f in real_files:
            content = await f.read()
            if len(content) > MAX_FILE_BYTES:
                continue
            stored_name = f"{uuid.uuid4().hex}_{_safe_filename(f.filename)}"
            dest = udir / stored_name
            async with aiofiles.open(dest, "wb") as fh:
                await fh.write(content)
            db.add(NoteFile(
                note_id=note.id,
                original_name=f.filename,
                stored_name=stored_name,
                file_size=len(content),
            ))

    db.commit()
    db.refresh(note)

    return JSONResponse({
        "ok":   True,
        "text": note.note_text,
        "files": [
            {"id": nf.id, "name": nf.original_name, "size": _fmt_size(nf.file_size)}
            for nf in note.files
        ],
    })


# --------------------------------------------------------------------------- #
#  Delete Single File from Note (AJAX)                                         #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/files/{file_id}/delete")
def delete_note_file(
    patient_id: int,
    file_id: int,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    nf = db.query(NoteFile).join(PatientNote).filter(
        NoteFile.id == file_id,
        PatientNote.patient_id == patient_id,
        PatientNote.doctor_id == doctor.id,
    ).first()
    if not nf:
        return JSONResponse({"error": "File not found."}, status_code=404)

    path = Path(f"uploads/patients/{doctor.id}/{patient_id}/{nf.stored_name}")
    if path.exists():
        path.unlink()

    db.delete(nf)
    db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
#  Delete Patient                                                               #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/delete")
def delete_patient(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if patient:
        # Remove upload directory for this patient
        import shutil
        udir = Path(f"uploads/patients/{doctor.id}/{patient_id}")
        if udir.exists():
            shutil.rmtree(udir, ignore_errors=True)

        # Delete all child records in dependency order to avoid FK errors
        # 1. NoteFiles attached to this patient's notes
        note_ids = [n.id for n in db.query(PatientNote.id).filter(PatientNote.patient_id == patient.id)]
        if note_ids:
            db.query(NoteFile).filter(NoteFile.note_id.in_(note_ids)).delete(synchronize_session=False)

        # 2. Patient notes
        db.query(PatientNote).filter(PatientNote.patient_id == patient.id).delete(synchronize_session=False)

        # 3. Bill items attached to this patient's bills
        from database.models import Bill, BillItem, Visit
        bill_ids = [b.id for b in db.query(Bill.id).filter(Bill.patient_id == patient.id)]
        if bill_ids:
            db.query(BillItem).filter(BillItem.bill_id.in_(bill_ids)).delete(synchronize_session=False)

        # 4. Bills
        db.query(Bill).filter(Bill.patient_id == patient.id).delete(synchronize_session=False)

        # 5. Visits
        db.query(Visit).filter(Visit.patient_id == patient.id).delete(synchronize_session=False)

        # 6. Patient documents
        db.query(PatientDocument).filter(PatientDocument.patient_id == patient.id).delete(synchronize_session=False)

        # 7. Appointments
        db.query(Appointment).filter(Appointment.patient_id == patient.id).delete(synchronize_session=False)

        # 8. Finally delete the patient
        db.delete(patient)
        db.commit()
    return RedirectResponse(url="/patients", status_code=303)


# --------------------------------------------------------------------------- #
#  Edit Patient                                                                 #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/edit")
def edit_patient(
    patient_id: int,
    name: str = Form(...),
    phone: str = Form(...),
    age: Optional[int] = Form(None),
    gender: Optional[str] = Form(None),
    blood_group: Optional[str] = Form(None),
    allergies: Optional[str] = Form(None),
    preferred_contact: Optional[str] = Form(None),
    language_pref: Optional[str] = Form(None),
    wa_consent: Optional[str] = Form(None),   # checkbox: "on" if checked, None if unchecked
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    phone_clean = phone.strip()
    if not phone_clean.isdigit() or len(phone_clean) != 10:
        return RedirectResponse(url=f"/patients/{patient_id}?error=invalid_phone", status_code=303)
    if patient:
        patient.name              = " ".join(w.capitalize() for w in name.strip().split())
        patient.phone             = phone_clean
        patient.age               = age if age and age > 0 else None
        patient.gender            = gender if gender else None
        patient.blood_group       = blood_group.strip() if blood_group and blood_group.strip() else None
        patient.allergies         = allergies.strip() if allergies and allergies.strip() else None
        patient.preferred_contact = preferred_contact if preferred_contact else "phone"
        patient.language_pref     = language_pref if language_pref else "english"
        # WhatsApp consent: record timestamp only when consent is first given
        new_consent = wa_consent == "on"
        if new_consent and not patient.wa_consent:
            patient.wa_consent    = True
            patient.wa_consent_at = datetime.now()
        elif not new_consent:
            patient.wa_consent    = False
            # Keep wa_consent_at as an audit trail — do not erase it
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)


# --------------------------------------------------------------------------- #
#  WhatsApp Consent Toggle (AJAX-friendly quick toggle from patient card)      #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/wa-consent")
def toggle_wa_consent(
    patient_id: int,
    consent: str = Form(...),   # "1" to grant, "0" to revoke
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    """Quick consent toggle — called from patient detail page."""
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    granted = consent.strip() == "1"
    if granted and not patient.wa_consent:
        patient.wa_consent    = True
        patient.wa_consent_at = datetime.now()
    elif not granted:
        patient.wa_consent = False
    db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)


# --------------------------------------------------------------------------- #
#  Legacy single-note update (kept for backwards compat, now unused by UI)     #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  Document Vault                                                               #
# --------------------------------------------------------------------------- #

@router.get("/{patient_id}/vault", response_class=HTMLResponse)
def vault_page(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    docs = (
        db.query(PatientDocument)
        .filter(
            PatientDocument.patient_id == patient_id,
            PatientDocument.doctor_id  == doctor.id,
        )
        .order_by(PatientDocument.uploaded_at.desc())
        .all()
    )

    # Group by category preserving display order
    grouped: dict = {k: [] for k in DOCUMENT_CATEGORIES}
    for d in docs:
        cat = d.category if d.category in grouped else "other"
        grouped[cat].append(d)

    return templates.TemplateResponse(request, "patient_vault.html", {
        "doctor":       doctor,
        "patient":      patient,
        "grouped":      grouped,
        "categories":   DOCUMENT_CATEGORIES,
        "doc_count":    len(docs),
        "fmt_size":     _fmt_size,
        "active":       "patients",
        "pin_required": getattr(request.state, "pin_required", False),
    })


@router.post("/{patient_id}/vault/upload")
async def vault_upload(
    patient_id: int,
    category: str = Form("other"),
    description: str = Form(""),
    files: List[UploadFile] = File(default=[]),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    real_files = [f for f in files if f.filename]
    if not real_files:
        return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)

    upload_dir = _upload_dir(doctor.id, patient_id)
    cat = category if category in DOCUMENT_CATEGORIES else "other"

    for f in real_files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            continue
        safe   = _safe_filename(f.filename)
        stored = f"doc_{uuid.uuid4().hex}_{safe}"
        (upload_dir / stored).write_bytes(data)
        mime, _ = mimetypes.guess_type(f.filename)
        db.add(PatientDocument(
            doctor_id     = doctor.id,
            patient_id    = patient_id,
            original_name = f.filename,
            stored_name   = stored,
            file_size     = len(data),
            mime_type     = mime,
            category      = cat,
            description   = description.strip() or None,
        ))

    db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)


@router.get("/{patient_id}/vault/{doc_id}")
def vault_serve(
    patient_id: int,
    doc_id: int,
    download: bool = Query(default=False),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    doc = db.query(PatientDocument).filter(
        PatientDocument.id         == doc_id,
        PatientDocument.patient_id == patient_id,
        PatientDocument.doctor_id  == doctor.id,
    ).first()
    if not doc:
        return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)

    file_path = _upload_dir(doctor.id, patient_id) / doc.stored_name
    if not file_path.exists():
        return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)

    mime = doc.mime_type or "application/octet-stream"
    inline = (
        not download
        and (
            any(mime.startswith(p) for p in _INLINE_MIME_PREFIXES)
            or mime in _INLINE_MIME_EXACT
        )
    )
    from urllib.parse import quote
    disposition = "inline" if inline else "attachment"
    ascii_name  = doc.original_name.encode("ascii", errors="ignore").decode()
    encoded     = quote(doc.original_name, safe=" .()")
    headers = {
        "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(file_path), media_type=mime, headers=headers)


@router.post("/{patient_id}/vault/{doc_id}/delete", response_class=HTMLResponse)
def vault_delete(
    patient_id: int,
    doc_id: int,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    doc = db.query(PatientDocument).filter(
        PatientDocument.id         == doc_id,
        PatientDocument.patient_id == patient_id,
        PatientDocument.doctor_id  == doctor.id,
    ).first()
    if doc:
        file_path = _upload_dir(doctor.id, patient_id) / doc.stored_name
        if file_path.exists():
            file_path.unlink()
        db.delete(doc)
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)


@router.post("/{patient_id}/vault/{doc_id}/edit", response_class=HTMLResponse)
def vault_edit(
    patient_id: int,
    doc_id: int,
    category: str = Form("other"),
    description: str = Form(""),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    doc = db.query(PatientDocument).filter(
        PatientDocument.id         == doc_id,
        PatientDocument.patient_id == patient_id,
        PatientDocument.doctor_id  == doctor.id,
    ).first()
    if doc:
        doc.category    = category if category in DOCUMENT_CATEGORIES else "other"
        doc.description = description.strip() or None
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}/vault", status_code=303)


# --------------------------------------------------------------------------- #
#  Legacy single-note update (kept for backwards compat, now unused by UI)     #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes", response_class=HTMLResponse)
def update_notes(
    patient_id: int,
    notes: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if patient:
        patient.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)
