"""
routers/billing_ops.py — Visit billing + price catalog

Routes:
  GET  /visits/{id}/bill-prefill    JSON prefill data for the bill modal
  POST /visits/{id}/bill            Create bill + close visit
  GET  /bills/{id}                  Bill detail page
  GET  /price-catalog               JSON list of catalog items for doctor
  POST /price-catalog               Add a catalog item
  POST /price-catalog/{id}/delete   Remove a catalog item
  POST /price-catalog/{id}/pin      Toggle pinned (quick button in modal)
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Doctor, Patient, Visit, VisitStatus, Bill, BillItem,
    PriceCatalog, PaymentMode, ClinicDoctor, Appointment,
)
from services.auth_service import get_paying_doctor
import services.visit_service as vs

router = APIRouter(tags=["billing_ops"])
templates = Jinja2Templates(directory="templates")


# ── helpers ──────────────────────────────────────────────────────────────── #

def _get_primary_clinic(doctor: Doctor, db: Session):
    m = db.query(ClinicDoctor).filter(
        ClinicDoctor.doctor_id == doctor.id,
        ClinicDoctor.is_active == True,
    ).first()
    return m.clinic if m else None


def _get_visit(visit_id: int, doctor_id: int, db: Session) -> Optional[Visit]:
    return db.query(Visit).filter(
        Visit.id == visit_id,
        Visit.doctor_id == doctor_id,
    ).first()


def _auto_complete_appointment(db: Session, visit: Visit):
    from database.models import AppointmentStatus
    if visit.appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        if appt and appt.status == AppointmentStatus.scheduled:
            appt.status = AppointmentStatus.completed


# ── Bill prefill JSON ─────────────────────────────────────────────────────── #

@router.get("/visits/{visit_id}/bill-prefill", response_class=JSONResponse)
async def bill_prefill(
    visit_id: int,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if not visit:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Pinned catalog items as quick buttons
    pinned = (
        db.query(PriceCatalog)
        .filter(
            PriceCatalog.doctor_id == doctor.id,
            PriceCatalog.is_active == True,
            PriceCatalog.is_pinned == True,
        )
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    # All active catalog items for the dropdown
    all_items = (
        db.query(PriceCatalog)
        .filter(
            PriceCatalog.doctor_id == doctor.id,
            PriceCatalog.is_active == True,
        )
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    # Guess default fee from appointment type if available
    default_fee = 0.0
    if visit.appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        if appt:
            # Try to find a matching catalog item by appointment type name
            type_name = appt.appointment_type.value.replace("_", " ").title()
            match = next((i for i in all_items if type_name.lower() in i.name.lower()), None)
            if match:
                default_fee = float(match.default_price)

    return JSONResponse({
        "visit_id":    visit.id,
        "patient_name": visit.patient.name,
        "default_fee": default_fee,
        "pinned": [{"id": i.id, "name": i.name, "price": float(i.default_price)} for i in pinned],
        "catalog": [{"id": i.id, "name": i.name, "price": float(i.default_price)} for i in all_items],
    })


# ── Create bill + close visit ─────────────────────────────────────────────── #

@router.post("/visits/{visit_id}/bill")
async def create_bill(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    form = await request.form()

    visit = _get_visit(visit_id, doctor.id, db)
    if not visit or visit.status != VisitStatus.billing_pending:
        return RedirectResponse("/appointments", status_code=303)

    primary_clinic = _get_primary_clinic(doctor, db)

    try:
        fee = float(form.get("fee") or 0)
    except ValueError:
        fee = 0.0
    try:
        discount = float(form.get("discount") or 0)
    except ValueError:
        discount = 0.0
    try:
        gst_amount = float(form.get("gst_amount") or 0)
    except ValueError:
        gst_amount = 0.0

    payment_mode_str = form.get("payment_mode", "cash")
    notes = (form.get("notes") or "").strip()

    # Collect line items — item_name[], item_price[], item_qty[]
    item_names      = form.getlist("item_name")
    item_prices_raw = form.getlist("item_price")
    item_qtys_raw   = form.getlist("item_qty")

    items = []
    items_subtotal = 0.0
    for i, (name, price_raw) in enumerate(zip(item_names, item_prices_raw)):
        name = name.strip()
        if not name:
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = max(1, int(float(item_qtys_raw[i]))) if i < len(item_qtys_raw) else 1
        except (ValueError, TypeError):
            qty = 1
        line_total = price * qty
        items.append((name, qty, price, line_total))
        items_subtotal += line_total

    subtotal = items_subtotal if items else fee
    disc     = min(discount, subtotal)
    total    = max(0.0, subtotal - disc + gst_amount)

    try:
        mode = PaymentMode(payment_mode_str)
    except ValueError:
        mode = PaymentMode.cash

    bill = Bill(
        visit_id     = visit.id,
        doctor_id    = doctor.id,
        clinic_id    = primary_clinic.id if primary_clinic else None,
        patient_id   = visit.patient_id,
        subtotal     = subtotal,
        discount     = disc,
        gst_amount   = gst_amount,
        total        = total,
        paid_amount  = total,
        payment_mode = mode,
        paid_at      = datetime.now(),
        notes        = notes or None,
        created_by   = doctor.id,
    )
    db.add(bill)
    db.flush()

    for name, qty, unit_price, line_total in items:
        db.add(BillItem(
            bill_id     = bill.id,
            description = name,
            quantity    = qty,
            unit_price  = unit_price,
            total       = line_total,
        ))

    _auto_complete_appointment(db, visit)
    vs.close_visit(db, visit, bill.id)

    # Re-fetch bill after close_visit commits so all relationships are fresh
    try:
        import logging as _log
        from services.bill_pdf_service import generate_and_store_bill_pdf
        fresh_bill = db.query(Bill).filter(Bill.id == bill.id).first()
        if fresh_bill:
            generate_and_store_bill_pdf(fresh_bill, db)
    except Exception as _pdf_err:
        _log.getLogger(__name__).error(f"PDF generation failed for bill {bill.id}: {_pdf_err}", exc_info=True)
    try:
        from services.notification_service import notify_bill_receipt
        notify_bill_receipt(bill, doctor, db)
    except Exception:
        pass

    return RedirectResponse("/appointments", status_code=303)


# ── Edit bill ────────────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
async def edit_bill_page(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    price_catalog = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    return templates.TemplateResponse(request, "bill_edit.html", {
        "active":        "appointments",
        "doctor":        doctor,
        "bill":          bill,
        "price_catalog": price_catalog,
    })


@router.post("/bills/{bill_id}/edit")
async def edit_bill(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    form = await request.form()

    try:
        discount = float(form.get("discount") or 0)
    except ValueError:
        discount = 0.0
    try:
        gst_amount = float(form.get("gst_amount") or 0)
    except ValueError:
        gst_amount = 0.0

    payment_mode_str = form.get("payment_mode", "cash")
    notes = (form.get("notes") or "").strip()

    item_names      = form.getlist("item_name")
    item_prices_raw = form.getlist("item_price")
    item_qtys_raw   = form.getlist("item_qty")

    items = []
    items_subtotal = 0.0
    for i, (name, price_raw) in enumerate(zip(item_names, item_prices_raw)):
        name = name.strip()
        if not name:
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = max(1, int(float(item_qtys_raw[i]))) if i < len(item_qtys_raw) else 1
        except (ValueError, TypeError):
            qty = 1
        line_total = price * qty
        items.append((name, qty, price, line_total))
        items_subtotal += line_total

    subtotal = items_subtotal if items else float(bill.subtotal)
    disc     = min(discount, subtotal)
    total    = max(0.0, subtotal - disc + gst_amount)

    try:
        mode = PaymentMode(payment_mode_str)
    except ValueError:
        mode = PaymentMode.cash

    bill.subtotal     = subtotal
    bill.discount     = disc
    bill.gst_amount   = gst_amount
    bill.total        = total
    bill.paid_amount  = total
    bill.payment_mode = mode
    bill.notes        = notes or None

    # Replace all items
    for item in list(bill.items):
        db.delete(item)
    db.flush()
    for name, qty, unit_price, line_total in items:
        db.add(BillItem(
            bill_id     = bill.id,
            description = name,
            quantity    = qty,
            unit_price  = unit_price,
            total       = line_total,
        ))

    db.commit()

    import logging as _logging
    _log = _logging.getLogger("billing_ops")
    try:
        from services.bill_pdf_service import regenerate_bill_pdf
        _log.info(f"regenerate_bill_pdf: starting for bill {bill.id}")
        regenerate_bill_pdf(bill, db)
        _log.info(f"regenerate_bill_pdf: done for bill {bill.id}")
    except Exception as _e:
        _log.error(f"regenerate_bill_pdf error: {_e}", exc_info=True)
    try:
        from services.notification_service import notify_bill_receipt
        notify_bill_receipt(bill, doctor, db)
    except Exception:
        pass

    return RedirectResponse(f"/patients/{bill.patient_id}", status_code=303)


# ── Mark bill as paid (from pending-collections) ─────────────────────────── #

@router.post("/bills/{bill_id}/mark-paid")
async def mark_bill_paid(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if bill and bill.paid_amount == 0:
        bill.paid_amount  = bill.total
        bill.payment_mode = bill.payment_mode or PaymentMode.cash
        bill.paid_at      = datetime.now()
        db.commit()
        try:
            from services.notification_service import notify_bill_receipt
            notify_bill_receipt(bill, doctor, db)
        except Exception:
            pass
    return RedirectResponse("/income", status_code=303)


# ── Bill detail page ──────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}", response_class=HTMLResponse)
async def bill_detail(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    # Auto-backfill: if no vault PDF exists for this bill, generate it now
    from database.models import PatientDocument
    has_pdf = db.query(PatientDocument).filter(
        PatientDocument.doctor_id  == bill.doctor_id,
        PatientDocument.patient_id == bill.patient_id,
        PatientDocument.stored_name.like(f"bill_{bill.id}_%"),
    ).first()
    if not has_pdf:
        try:
            from services.bill_pdf_service import generate_and_store_bill_pdf
            generate_and_store_bill_pdf(bill, db)
        except Exception:
            pass

    return templates.TemplateResponse(request, "bill_detail.html", {
        "active": "appointments",
        "doctor": doctor,
        "bill":   bill,
    })


# ── Bill PDF download ─────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}/pdf")
async def download_bill_pdf(
    bill_id: int,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    try:
        from services.bill_pdf_service import _build_pdf
        patient = db.query(Patient).filter(Patient.id == bill.patient_id).first()
        visit   = bill.visit
        appt    = None
        if visit and visit.appointment_id:
            appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        items   = list(bill.items)
        pdf     = _build_pdf(bill, patient, doctor, visit, appt, items)
        data    = bytes(pdf.output())
        fname   = f"bill_{bill.id}_{patient.name.replace(' ','_') if patient else 'receipt'}.pdf"
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception:
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)


# ── Price catalog CRUD ────────────────────────────────────────────────────── #

@router.get("/price-catalog", response_class=JSONResponse)
async def get_catalog(
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    items = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )
    return [{"id": i.id, "name": i.name, "price": float(i.default_price), "pinned": i.is_pinned} for i in items]


@router.post("/price-catalog")
async def add_catalog_item(
    request: Request,
    name: str   = Form(...),
    price: float = Form(...),
    pinned: bool = Form(False),
    db: Session  = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    item = PriceCatalog(
        doctor_id     = doctor.id,
        name          = name.strip(),
        default_price = price,
        is_pinned     = pinned,
        is_active     = True,
    )
    db.add(item)
    db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)


@router.post("/price-catalog/{item_id}/delete")
async def delete_catalog_item(
    item_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    item = db.query(PriceCatalog).filter(
        PriceCatalog.id == item_id,
        PriceCatalog.doctor_id == doctor.id,
    ).first()
    if item:
        item.is_active = False
        db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)


@router.post("/price-catalog/{item_id}/pin")
async def toggle_pin(
    item_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    item = db.query(PriceCatalog).filter(
        PriceCatalog.id == item_id,
        PriceCatalog.doctor_id == doctor.id,
    ).first()
    if item:
        item.is_pinned = not item.is_pinned
        db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)
