import re
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Doctor, PlanType, Clinic, ClinicDoctor, ClinicDoctorInvite
from config import settings
from services.auth_service import hash_password, verify_password, create_access_token, decode_token

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="templates")


def _make_slug(name: str, city: str) -> str:
    """Generate a URL-safe slug from doctor name + city."""
    raw = f"{name}-{city}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug


def _unique_slug(base: str, db: Session) -> str:
    slug = base
    counter = 1
    while db.query(Doctor).filter(Doctor.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# ------------------------------------------------------------------ #
#  Register                                                            #
# ------------------------------------------------------------------ #

@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    clinic_invite: str = Query(default=""),
    plan: str = Query(default=""),
    db: Session = Depends(get_db),
):
    # Redirect already-logged-in users away from register
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)

    joining_clinic = None
    if clinic_invite:
        invite = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == clinic_invite,
            ClinicDoctorInvite.used_at == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).first()
        if invite:
            joining_clinic = db.query(Clinic).filter(Clinic.id == invite.clinic_id).first()

    plan_hint = plan if plan in ("solo", "clinic") else "solo"
    return templates.TemplateResponse(request, "register.html", {
        "error": None,
        "clinic_invite": clinic_invite,
        "joining_clinic": joining_clinic,
        "plan_hint": plan_hint,
    })


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    specialization: str = Form(""),
    clinic_name: str = Form(""),
    city: str = Form(""),
    clinic_invite: str = Form(""),
    medical_reg_number: str = Form(""),
    db: Session = Depends(get_db),
):
    invite_token = clinic_invite.strip()

    # Check duplicates
    if db.query(Doctor).filter(Doctor.email == email).first():
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Email already registered. Please login.", "clinic_invite": invite_token,
             "joining_clinic": None, "plan_hint": "solo"},
            status_code=400,
        )
    if db.query(Doctor).filter(Doctor.phone == phone).first():
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Phone number already registered.", "clinic_invite": invite_token,
             "joining_clinic": None, "plan_hint": "solo"},
            status_code=400,
        )

    slug = _unique_slug(_make_slug(name, city or "clinic"), db)

    # Check for valid clinic invite BEFORE creating the doctor
    valid_invite = None
    if invite_token:
        valid_invite = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == invite_token,
            ClinicDoctorInvite.used_at == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).first()

    if valid_invite:
        # ── Clinic member path: no trial, no solo clinic ──────────────────────
        doctor = Doctor(
            name=name,
            email=email.lower().strip(),
            phone=phone.strip(),
            password_hash=hash_password(password),
            specialization=specialization.strip() or None,
            clinic_name=None,    # will show joined clinic name from Clinic table
            city=city.strip() or None,
            slug=slug,
            plan_type=PlanType.trial,
            trial_ends_at=None,  # no trial — access gated by clinic plan
            plan_expires_at=None,
            medical_reg_number=medical_reg_number.strip() or None,
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)

        db.add(ClinicDoctor(
            clinic_id=valid_invite.clinic_id,
            doctor_id=doctor.id,
            role="associate",
            is_active=True,
        ))
        valid_invite.used_at = datetime.utcnow()
        db.commit()

    else:
        # ── Solo doctor path: 14-day trial + auto solo clinic ─────────────────
        doctor = Doctor(
            name=name,
            email=email.lower().strip(),
            phone=phone.strip(),
            password_hash=hash_password(password),
            specialization=specialization.strip() or None,
            clinic_name=clinic_name.strip() or None,
            city=city.strip() or None,
            slug=slug,
            plan_type=PlanType.trial,
            trial_ends_at=datetime.utcnow() + timedelta(days=14),
            medical_reg_number=medical_reg_number.strip() or None,
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)

        # Auto-create an implicit clinic for every solo doctor (owner role)
        clinic_slug = slug + "-clinic"
        base_clinic_slug = clinic_slug
        counter = 1
        while db.query(Clinic).filter(Clinic.slug == clinic_slug).first():
            clinic_slug = f"{base_clinic_slug}-{counter}"
            counter += 1

        clinic = Clinic(
            name=clinic_name.strip() or f"{name}'s Clinic",
            address=None,
            city=city.strip() or None,
            slug=clinic_slug,
            plan_type="trial",
            owner_doctor_id=doctor.id,
        )
        db.add(clinic)
        db.commit()
        db.refresh(clinic)

        db.add(ClinicDoctor(
            clinic_id=clinic.id,
            doctor_id=doctor.id,
            role="owner",
            is_active=True,
        ))
        db.commit()

    return RedirectResponse(url="/login?registered=1", status_code=303)


# ------------------------------------------------------------------ #
#  Login                                                               #
# ------------------------------------------------------------------ #

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, registered: str = "", next: str = ""):
    # Redirect already-logged-in users away from login
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    success = "Account created! Please log in." if registered == "1" else None
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "success": success, "next": next,
    })


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    normalized_email = email.lower().strip()

    # ── Try doctor first ──────────────────────────────────────────────────────
    doctor = db.query(Doctor).filter(Doctor.email == normalized_email).first()
    if doctor:
        if not verify_password(password, doctor.password_hash):
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid email or password.", "success": None, "next": next},
                status_code=401,
            )
        if not doctor.is_active:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Your account has been deactivated.", "success": None, "next": next},
                status_code=403,
            )
        token = create_access_token({"doctor_id": doctor.id})
        # Honor the `next` param — only relative paths, no open redirect
        safe_next = next.strip() if (
            next and next.startswith("/") and not next.startswith("//")
            and not next.startswith("/login") and not next.startswith("/register")
        ) else ""
        redirect_url = safe_next if safe_next else "/workspace-loading"
        response = RedirectResponse(url=redirect_url, status_code=303)
        response.set_cookie(
            key="access_token", value=token,
            httponly=True, secure=settings.is_production, max_age=60 * 60 * 24, samesite="lax",
        )
        return response

    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Invalid email or password.", "success": None, "next": next},
        status_code=401,
    )


# ------------------------------------------------------------------ #
#  Logout                                                              #
# ------------------------------------------------------------------ #

@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response
