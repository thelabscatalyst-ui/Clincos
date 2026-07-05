"""
routers/feedback.py — Public patient feedback / rating.

  GET  /feedback/{token}   — minimal star-rating form (no login)
  POST /feedback/{token}   — save rating + review

The token is created when a bill receipt is sent (see notification_service).
"""
from datetime import datetime

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Feedback, Doctor

router = APIRouter(tags=["feedback"])
templates = Jinja2Templates(directory="templates")


@router.get("/feedback/{token}", response_class=HTMLResponse)
def feedback_form(token: str, request: Request, db: Session = Depends(get_db)):
    fb = db.query(Feedback).filter(Feedback.token == token).first()
    if not fb:
        return templates.TemplateResponse(
            request, "feedback.html", {"invalid": True, "clinic_name": "the clinic"},
            status_code=404,
        )
    doctor = db.query(Doctor).filter(Doctor.id == fb.doctor_id).first()
    clinic_name = (doctor.clinic_name if doctor and doctor.clinic_name
                   else (f"Dr. {doctor.name}" if doctor else "the clinic"))
    return templates.TemplateResponse(request, "feedback.html", {
        "invalid":     False,
        "fb":          fb,
        "clinic_name": clinic_name,
        "submitted":   fb.rating is not None,
    })


@router.post("/feedback/{token}", response_class=HTMLResponse)
def feedback_submit(
    token: str,
    request: Request,
    rating: int = Form(...),
    review: str = Form(""),
    db: Session = Depends(get_db),
):
    fb = db.query(Feedback).filter(Feedback.token == token).first()
    if not fb:
        return templates.TemplateResponse(
            request, "feedback.html", {"invalid": True, "clinic_name": "the clinic"},
            status_code=404,
        )
    doctor = db.query(Doctor).filter(Doctor.id == fb.doctor_id).first()
    clinic_name = (doctor.clinic_name if doctor and doctor.clinic_name
                   else (f"Dr. {doctor.name}" if doctor else "the clinic"))

    # Only accept the first submission; clamp rating to 1–5.
    if fb.rating is None:
        fb.rating       = max(1, min(5, int(rating)))
        fb.review       = (review or "").strip()[:2000] or None
        fb.submitted_at = datetime.utcnow()
        db.commit()

    return templates.TemplateResponse(request, "feedback.html", {
        "invalid":     False,
        "fb":          fb,
        "clinic_name": clinic_name,
        "submitted":   True,
        "just_saved":  True,
    })
