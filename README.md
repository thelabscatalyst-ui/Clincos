# ClinicOS

Appointment and clinic management SaaS for independent doctors in Indian Tier 2/3 cities.

Doctors get a personal booking page, live token queue, WhatsApp reminders, patient records, billing, and an income dashboard — all for ₹399/month. No app download needed for patients. Zero setup.

---

## Features

### Appointments & Queue
- Live token queue on the appointments page — check in walk-ins, serve, skip, promote emergencies
- Queue state machine: Waiting → Serving → Billing Pending → Done
- Walk-in quick booking with auto check-in to queue
- Returning patient detection — entering a known phone number shows a **Returning** badge with visit count and auto-fills the patient's name
- Scheduled appointments with slot-based booking (no double booking)
- Create, edit, and reschedule appointments
- Appointment detail card overlay — view history, doctor notes, status
- Booking channel badges: Walk-in · Doctor · Patient · Reception
- Monthly calendar view with today's date indicator
- Public TV display screen at `/queue/{slug}` — shows who is currently being served
- Today's Flow bar at the top of the appointments page — live counts and on-time rate

### Billing & Income
- Collect Payment modal — line items, discount, payment mode (Cash / UPI / Card / Insurance / Free)
- Price catalog in Settings — pre-set consultation fees as quick-fill buttons in the billing modal
- Edit bill after saving — update items, discount, payment mode
- PDF bill auto-generated on payment and saved to the patient's document vault
- Income dashboard — daily and monthly revenue with charts and transaction history
- Expense tracker — log clinic expenses by category with recurring expense support
- All income and billing pages are PIN-protected

### Patients
- Patient list with search
- Patient profile — visit history, doctor notes, age, gender, blood group, allergies
- Edit name, phone, and medical details
- Delete patient (PIN-protected, removes all linked records)
- Pre-fill booking form from patient profile
- Document vault per patient — upload, categorise, and search files; auto-invoices included

### Settings & Security
- Configurable working hours, slot duration, and max patients per day of week
- Block specific dates and time windows
- 6-digit PIN protection for sensitive sections: Income, Reports, Billing, Settings, Patient detail
- One login shared with receptionist — PIN locks the sensitive sections only
- Account details edit — name, email, phone, specialization
- Dark / light theme toggle persisted in localStorage

### Reports
- Completion and no-show rates
- Top patients by visit count
- Monthly appointment trend (curved chart)
- PIN-protected

### Notifications
- WhatsApp confirmation immediately after booking
- Automatic reminders 24 hours and 2 hours before appointment
- Falls back to SMS if WhatsApp is unavailable
- Walk-in bookings skip the confirmation notification

### Public Booking (Patients)
- Book via a personal URL — no login, no app download
- WhatsApp confirmation sent immediately after booking
- Google Calendar link on confirmation page
- Rate-limited (max 5 bookings per phone per 24 hours)

### Clinic Plan (Multi-Doctor)
- Multiple doctors under one clinic
- Clinic admin dashboard — aggregated stats, doctor schedules, today's appointments across all doctors
- Unified public booking page at `/book/clinic/{slug}`
- Clinic plan billing separate from individual doctor plans
- Associate doctors' access is covered by the clinic owner's plan

### Platform Admin
- Admin panel at `/admin` — all registered doctors, active trials, paid plans, expired accounts
- Platform-level revenue overview

---

## Tech Stack

| Layer | Tool |
|---|---|
| Backend | FastAPI (Python 3.14) |
| Templates | Jinja2 (server-side rendering) |
| Frontend | HTML + CSS + Vanilla JS (no React, no Vue) |
| Database (dev) | SQLite |
| Database (prod) | PostgreSQL |
| ORM | SQLAlchemy |
| Auth | JWT in HTTP-only cookie (Passlib + bcrypt 4.0.1) |
| Notifications | Twilio WhatsApp + SMS |
| Payments | Razorpay (UPI, cards, net banking) |
| Scheduler | APScheduler (background reminder jobs) |
| Deployment | Railway.app |

---

## Local Setup

**Prerequisites:** Python 3.10+

```bash
# 1. Clone and enter project
git clone <repo-url>
cd ClinicOS

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file (see Environment Variables below)
cp .env.example .env

# 5. Run
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000` — redirects to the login page.

If the port is in use: `kill $(lsof -ti:8000)` then restart.

---

## Environment Variables

```env
# Core
DATABASE_URL=sqlite:///./clinic.db
SECRET_KEY=replace-with-random-secret
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# Twilio — WhatsApp/SMS notifications
# Get from: console.twilio.com
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_SMS_FROM=                            # optional fallback SMS number

# Razorpay — payments
# Get from: dashboard.razorpay.com → Settings → API Keys
RAZORPAY_KEY_ID=rzp_test_XXXXXXXXXXXXXXXX
RAZORPAY_KEY_SECRET=XXXXXXXXXXXXXXXXXXXXXXXX

# Admin panel
# Must match the email used to register the admin doctor account
ADMIN_EMAIL=your-email@example.com
```

Generate a strong `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Twilio and Razorpay are optional for local development — the app runs without them. Notifications log as `failed` and the payment button shows "not configured".

---

## Project Structure

```
ClinicOS/
├── main.py                      # App entry point — routers, middleware, exception handlers
├── config.py                    # Settings loaded from .env via pydantic-settings
├── requirements.txt
├── Procfile                     # Railway: uvicorn main:app --host 0.0.0.0 --port $PORT
│
├── database/
│   ├── connection.py            # Engine, SessionLocal, create_tables(), _run_migrations()
│   └── models.py                # All ORM models and enums
│
├── routers/
│   ├── auth.py                  # /register, /login, /logout
│   ├── doctors.py               # /dashboard, /calendar, /reports, /billing, /doctors/settings/*, /pin-prompt
│   ├── appointments.py          # /appointments — queue + schedule, CRUD, walk-in
│   ├── visits.py                # /visits/* — queue state machine, public display screen
│   ├── billing_ops.py           # /visits/{id}/bill, /bills/*, /price-catalog
│   ├── income.py                # /income, /expenses — revenue dashboard + expense tracker
│   ├── patients.py              # /patients — list, profiles, notes, vault, delete
│   ├── clinic.py                # /clinic/admin/*, /doctor-invite/* — multi-doctor clinic
│   ├── public.py                # /book/{slug} — public booking (no auth, rate-limited)
│   └── admin.py                 # /admin — platform owner only
│
├── services/
│   ├── auth_service.py          # JWT auth, PIN session, all get_*_doctor dependencies
│   ├── appointment_service.py   # Slot availability, patient upsert
│   ├── visit_service.py         # Queue logic — check_in, call_next, done, skip, emergency
│   ├── notification_service.py  # Twilio WhatsApp + SMS
│   ├── payment_service.py       # Razorpay order create + HMAC signature verify
│   ├── bill_pdf_service.py      # fpdf2 PDF bill generation → auto-saved to patient vault
│   └── scheduler_service.py     # APScheduler — T-24h and T-2h reminder jobs
│
├── templates/                   # Jinja2 HTML templates
│   ├── base.html                # Master layout — navbar, dock (7 items), PIN overlay
│   ├── clinic/                  # Clinic admin templates
│   └── ...
│
├── static/
│   └── css/main.css             # Warm sepia/parchment design system — dark + light themes
│
├── uploads/                     # Patient document vault files (gitignored)
│   └── patients/{doctor_id}/{patient_id}/
│
└── docs/
    └── design-tokens.md         # Design system source of truth — colors, spacing, typography
```

---

## Queue / Token System

Appointments and walk-ins flow through a live queue on the Appointments page:

```
Walk-in check-in  ──→  WAITING  ──→  SERVING  ──→  BILLING PENDING  ──→  DONE
                            ↓              ↓
                         SKIPPED       CANCELLED
```

- **Token number** — auto-assigned per doctor per day, monotonically increasing
- **Queue position** — mutable; reordered by Skip, Emergency, and Move actions
- **Emergency** — promotes a patient to the front of the waiting queue
- **Done** — moves to Billing Pending and auto-calls the next waiting patient
- **Free / Close** — closes visit with zero charge, bypasses billing
- **Public display** — `/queue/{slug}` shows the current serving token on a TV screen, auto-refreshing

---

## PIN Protection

One doctor account is shared with the receptionist at the desk. Sensitive sections are locked behind a 6-digit PIN set by the doctor:

| Locked (PIN required) | Open (no PIN needed) |
|---|---|
| Income & Revenue | Appointments & Queue |
| Reports & Analytics | Walk-in check-in |
| Billing & Subscription | Patient list |
| Settings | Calendar |
| Patient detail | Add expenses |

The PIN session lasts 30 minutes. Wrong PIN shows an error on the blur overlay without redirecting.

---

## How Notifications Work

1. Appointment booked → WhatsApp confirmation sent immediately
2. Falls back to SMS if `TWILIO_SMS_FROM` is set and WhatsApp fails
3. Background scheduler runs every 15 minutes:
   - Sends 24-hour reminder when appointment is 23–25 hours away
   - Sends 2-hour reminder when appointment is 90–150 minutes away
4. Every send logged in `notifications_log` table with `sent` or `failed` status
5. Walk-in bookings skip the confirmation (patient is already at the clinic)
6. All notification calls are wrapped in `try/except` — a failure never blocks a booking

---

## How Payments Work

1. Doctor clicks Subscribe → `POST /billing/create-order?plan=solo|clinic`
2. Razorpay checkout popup opens in-browser (loaded from CDN)
3. Patient completes UPI / card payment
4. `POST /billing/verify` — server verifies HMAC-SHA256 signature
5. On success: `doctor.plan_expires_at = now + 30 days`, subscription row created
6. For clinic plan: `clinic.plan_type = 'clinic'`, `clinic.plan_expires_at` set

Use Razorpay test keys for development — no real charges.

---

## Subscription Plans

| Plan | Price | For |
|---|---|---|
| Free Trial | 14 days | Full access, no card needed |
| Solo | ₹399/month | Individual doctor |
| Clinic | ₹1,499/month | Multi-doctor clinic |

Solo doctors see only the Solo plan in Settings. Clinic owners see only the Clinic plan. Associate doctors see a "managed by clinic" notice — no billing required from them.

---

## Deployment

**Stack: Fly.io (app) + Neon.tech (PostgreSQL) + Cloudflare R2 (file storage)**

| Service | Role | Free tier |
|---|---|---|
| [Fly.io](https://fly.io) | App hosting — always-on ASGI container | 3 shared VMs free |
| [Neon.tech](https://neon.tech) | PostgreSQL database | 512MB free, no expiry |
| [Cloudflare R2](https://cloudflare.com/r2) | Patient file vault storage | 10GB free, zero egress fees |

> **Note:** Vercel is not suitable — ClinicOS uses APScheduler (requires a persistent process) and writes files to disk (serverless has no persistent filesystem). Railway.app was considered but storage costs ($0.25/GB/month extra) are not practical for a growing patient document vault.

**Deploy to Fly.io:**
```bash
fly launch
fly secrets set DATABASE_URL=... SECRET_KEY=... # set all env vars
fly deploy
```

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## Design System

Warm sepia / parchment palette — both dark and light themes share a brown-amber aesthetic. No paper texture, no ambient glow — clean flat cards with drop shadows.

| Theme | Background | Cards | Text |
|---|---|---|---|
| Dark (default) | `#1a1612` | `#211d18` | `#ede8e2` |
| Light | `#e6e0d7` | `#f5f2ec` | `#1a1410` |

- **Fonts** — `Inter` for all body text; `Playfair Display` only for the brand logo and page titles
- **Border radius** — `--radius-xs: 6px` · `--radius-sm: 8px` · `--radius: 16px` · `--radius-lg: 24px`
- Navbar and dock are always dark brown (`#2e1e0c`) regardless of theme
- Full token reference in `docs/design-tokens.md`

---

## License

Private — all rights reserved.
