"""
Demo seed for Ananya Mehta (doctor_id=1) — today only.
Covers: appointments, walk-ins, queue states, billing, expenses,
        doctor notes, follow-up scheduling, patient notes, price catalog.
Run once: python seed_demo.py
"""
from datetime import date, time, datetime, timedelta
from database.connection import SessionLocal
from database.models import (
    Doctor, Patient, Appointment, AppointmentStatus, AppointmentType,
    BookedBy, Visit, VisitStatus, VisitSource, Bill, BillItem,
    PaymentMode, PriceCatalog, Expense, ExpenseCategory,
)

db = SessionLocal()
DOCTOR_ID = 1
TODAY = date.today()
NOW = datetime.now()


# ── 1. Price catalog ────────────────────────────────────────────────────────
catalog_items = [
    ("Consultation Fee",        400, True),
    ("Follow-up Consultation",  250, True),
    ("Blood Pressure Check",     80, True),
    ("ECG",                     350, False),
    ("Dressing & Wound Care",   200, False),
    ("Injection Administration", 150, False),
    ("Nebulisation",            180, False),
    ("Urine Routine Test",      120, False),
]
catalog_map = {}
for name, price, pinned in catalog_items:
    pc = PriceCatalog(doctor_id=DOCTOR_ID, name=name, default_price=price,
                      is_active=True, is_pinned=pinned)
    db.add(pc)
db.flush()
for pc in db.query(PriceCatalog).filter(PriceCatalog.doctor_id == DOCTOR_ID).all():
    catalog_map[pc.name] = pc
print(f"  ✓ {len(catalog_map)} catalog items")


# ── 2. Patients ─────────────────────────────────────────────────────────────
patients_data = [
    # name, phone, age, gender, blood_group, allergies, notes
    ("Ramesh Patil",   "9823001001", 52, "Male",   "B+",  None,        "Hypertensive, on Amlodipine 5mg"),
    ("Sunita Desai",   "9823001002", 34, "Female", "O+",  "Penicillin","Diabetic Type 2, HbA1c 7.2"),
    ("Arjun Nair",     "9823001003", 28, "Male",   "A+",  None,        "Asthma, uses Salbutamol inhaler"),
    ("Kavita Sharma",  "9823001004", 45, "Female", "AB+", None,        "Thyroid – on Levothyroxine 50mcg"),
    ("Mohan Kulkarni", "9823001005", 61, "Male",   "O-",  "Sulfa",     "Post MI 2024, on Aspirin + Atorvastatin"),
    ("Priya Joshi",    "9823001006", 22, "Female", "B-",  None,        "Migraine – triptans prescribed earlier"),
    ("Deepak Rao",     "9823001007", 38, "Male",   "A-",  None,        "Kidney stone history, 2022"),
    ("Meena Iyer",     "9823001008", 55, "Female", "O+",  "Aspirin",   "Osteoarthritis both knees"),
    ("Sahil Khan",     "9823001009", 19, "Male",   "B+",  None,        "Sports injury, right ankle"),
    ("Lata Bhosale",   "9823001010", 67, "Female", "A+",  None,        "COPD – on Tiotropium inhaler"),
    ("Vivek Pawar",    "9823001011", 41, "Male",   "AB-", None,        "Fatty liver, on diet management"),
    ("Anjali Gupta",   "9823001012", 30, "Female", "O+",  None,        "PCOS, irregular cycles"),
]

patients = []
for name, phone, age, gender, bg, allergy, notes in patients_data:
    p = Patient(
        doctor_id=DOCTOR_ID,
        name=name, phone=phone, age=age, gender=gender,
        blood_group=bg, allergies=allergy, notes=notes,
        visit_count=0,
        first_visit=TODAY, last_visit=TODAY,
    )
    db.add(p)
    patients.append(p)
db.flush()
print(f"  ✓ {len(patients)} patients")


# ── helpers ──────────────────────────────────────────────────────────────────
_token_counter = [0]
def next_token():
    _token_counter[0] += 1
    return _token_counter[0]

def make_appt(patient, appt_time, appt_type=AppointmentType.follow_up,
              booked_by=BookedBy.doctor, doctor_notes="", status=AppointmentStatus.scheduled):
    a = Appointment(
        doctor_id=DOCTOR_ID,
        patient_id=patient.id,
        appointment_date=TODAY,
        appointment_time=appt_time,
        duration_mins=15,
        appointment_type=appt_type,
        status=status,
        booked_by=booked_by,
        doctor_notes=doctor_notes,
        reminder_24h_sent=True,
        reminder_2h_sent=True,
    )
    db.add(a)
    db.flush()
    return a

def make_visit(appt, source=VisitSource.appointment, status=VisitStatus.waiting,
               check_in=None, call_time=None, is_emergency=False):
    tok = next_token()
    v = Visit(
        doctor_id=DOCTOR_ID,
        patient_id=appt.patient_id,
        appointment_id=appt.id,
        visit_date=TODAY,
        token_number=tok,
        queue_position=tok,
        status=status,
        source=source,
        is_emergency=is_emergency,
        check_in_time=check_in or NOW - timedelta(minutes=90 - tok * 5),
        call_time=call_time,
        created_by=DOCTOR_ID,
    )
    db.add(v)
    db.flush()
    appt.visit_id = v.id
    appt.patient.visit_count = (appt.patient.visit_count or 0) + 1
    return v

def make_bill(visit, items, payment_mode=PaymentMode.cash, paid=True, discount=0):
    subtotal = sum(qty * price for _, qty, price in items)
    gst = 0
    total = subtotal - discount
    paid_amount = total if paid else 0
    b = Bill(
        visit_id=visit.id,
        doctor_id=DOCTOR_ID,
        patient_id=visit.patient_id,
        subtotal=subtotal,
        discount=discount,
        gst_amount=gst,
        total=total,
        paid_amount=paid_amount,
        payment_mode=payment_mode,
        paid_at=NOW - timedelta(minutes=30) if paid else None,
        notes="",
        created_by=DOCTOR_ID,
    )
    db.add(b)
    db.flush()
    for desc, qty, price in items:
        db.add(BillItem(bill_id=b.id, description=desc, quantity=qty,
                        unit_price=price, total=qty * price))
    visit.status = VisitStatus.done
    return b


# ── 3. Seed each patient's journey ─────────────────────────────────────────

# 1. Ramesh Patil — DONE + billed (consultation + BP check)
a1 = make_appt(patients[0], time(9, 0), AppointmentType.follow_up,
               doctor_notes="BP 150/90. Increased Amlodipine to 10mg. Review in 4 weeks.")
v1 = make_visit(a1, status=VisitStatus.done,
                check_in=NOW - timedelta(hours=2),
                call_time=NOW - timedelta(hours=1, minutes=50))
b1 = make_bill(v1, [("Follow-up Consultation", 1, 250), ("Blood Pressure Check", 1, 80)],
               payment_mode=PaymentMode.upi, paid=True)
a1.status = AppointmentStatus.completed

# 2. Sunita Desai — DONE + billed (consultation + urine test), follow-up booked
a2 = make_appt(patients[1], time(9, 15), AppointmentType.follow_up,
               doctor_notes="HbA1c improved. Continue Metformin 500mg BD. Urine routine normal.")
v2 = make_visit(a2, status=VisitStatus.done,
                check_in=NOW - timedelta(hours=1, minutes=55),
                call_time=NOW - timedelta(hours=1, minutes=45))
b2 = make_bill(v2, [("Follow-up Consultation", 1, 250), ("Urine Routine Test", 1, 120)],
               payment_mode=PaymentMode.cash, paid=True)
a2.status = AppointmentStatus.completed
# Book follow-up for Sunita 2 weeks out
follow_up_date = TODAY + timedelta(weeks=2)
fu = Appointment(
    doctor_id=DOCTOR_ID, patient_id=patients[1].id,
    appointment_date=follow_up_date,
    appointment_time=time(9, 15), duration_mins=15,
    appointment_type=AppointmentType.follow_up,
    status=AppointmentStatus.scheduled,
    booked_by=BookedBy.doctor,
    doctor_notes="",
    reminder_24h_sent=False, reminder_2h_sent=False,
)
db.add(fu)

# 3. Arjun Nair — DONE + billed (consultation + nebulisation), free (nebulisation)
a3 = make_appt(patients[2], time(9, 30), AppointmentType.new_patient,
               doctor_notes="Acute exacerbation. Nebulisation given. Salbutamol + Ipratropium. Follow up if not improving.")
v3 = make_visit(a3, status=VisitStatus.done,
                check_in=NOW - timedelta(hours=1, minutes=45),
                call_time=NOW - timedelta(hours=1, minutes=35))
b3 = make_bill(v3, [("Consultation Fee", 1, 400), ("Nebulisation", 1, 180)],
               payment_mode=PaymentMode.cash, paid=True, discount=50)
a3.status = AppointmentStatus.completed

# 4. Kavita Sharma — DONE + billed, ECG done
a4 = make_appt(patients[3], time(9, 45), AppointmentType.follow_up,
               doctor_notes="TSH 3.2 – within range. Continue current dose. ECG normal sinus rhythm.")
v4 = make_visit(a4, status=VisitStatus.done,
                check_in=NOW - timedelta(hours=1, minutes=30),
                call_time=NOW - timedelta(hours=1, minutes=20))
b4 = make_bill(v4, [("Follow-up Consultation", 1, 250), ("ECG", 1, 350)],
               payment_mode=PaymentMode.upi, paid=True)
a4.status = AppointmentStatus.completed

# 5. Mohan Kulkarni — BILLING PENDING (done but not billed yet)
a5 = make_appt(patients[4], time(10, 0), AppointmentType.follow_up,
               doctor_notes="Post MI follow-up. Lipid profile improving. No chest pain. Continue medications.")
v5 = make_visit(a5, status=VisitStatus.billing_pending,
                check_in=NOW - timedelta(minutes=60),
                call_time=NOW - timedelta(minutes=50))

# 6. Priya Joshi — SERVING (currently with doctor) — emergency
a6 = make_appt(patients[5], time(10, 15), AppointmentType.emergency,
               booked_by=BookedBy.doctor)
a6.is_emergency = True
v6 = make_visit(a6, status=VisitStatus.serving, is_emergency=True,
                check_in=NOW - timedelta(minutes=20),
                call_time=NOW - timedelta(minutes=10))

# 7. Deepak Rao — WAITING (in queue, checked in)
a7 = make_appt(patients[6], time(10, 30), AppointmentType.follow_up)
v7 = make_visit(a7, status=VisitStatus.waiting,
                check_in=NOW - timedelta(minutes=15))

# 8. Meena Iyer — WAITING (in queue)
a8 = make_appt(patients[7], time(10, 45), AppointmentType.follow_up)
v8 = make_visit(a8, status=VisitStatus.waiting,
                check_in=NOW - timedelta(minutes=10))

# 9. Sahil Khan — walk-in, WAITING
a9 = make_appt(patients[8], time(11, 0), AppointmentType.new_patient,
               booked_by=BookedBy.walk_in)
v9 = make_visit(a9, source=VisitSource.walk_in, status=VisitStatus.waiting,
                check_in=NOW - timedelta(minutes=8))

# 10. Lata Bhosale — SCHEDULED (upcoming, not checked in)
a10 = make_appt(patients[9], time(11, 30), AppointmentType.follow_up,
                booked_by=BookedBy.patient)

# 11. Vivek Pawar — SCHEDULED (upcoming, booked by doctor)
a11 = make_appt(patients[10], time(12, 0), AppointmentType.follow_up,
                doctor_notes="Repeat LFT ordered.")

# 12. Anjali Gupta — SCHEDULED (upcoming, walk-in style from reception)
a12 = make_appt(patients[11], time(12, 30), AppointmentType.new_patient,
                booked_by=BookedBy.staff_shared)

# 13. NO SHOW — Ramesh gets a second slot (simulate someone who didn't come)
ghost = Patient(
    doctor_id=DOCTOR_ID, name="Suresh Wagh", phone="9823001099",
    age=47, gender="Male", blood_group="O+",
    notes="Previous no-show. Chronic back pain.",
    visit_count=0, first_visit=TODAY, last_visit=TODAY,
)
db.add(ghost)
db.flush()
a_ns = make_appt(ghost, time(9, 0), AppointmentType.follow_up,
                 booked_by=BookedBy.patient, status=AppointmentStatus.no_show)

# ── 4. Expenses ──────────────────────────────────────────────────────────────
expenses = [
    (ExpenseCategory.misc,       "Gloves, syringes, cotton",        850,  TODAY),
    (ExpenseCategory.medicines,  "Salbutamol nebules restock",      1200,  TODAY),
    (ExpenseCategory.utilities,  "Electricity bill — May",          2400,  TODAY - timedelta(days=1)),
    (ExpenseCategory.misc,       "Cleaning supplies",                400,  TODAY - timedelta(days=2)),
    (ExpenseCategory.equipment,  "Stethoscope replacement",         2800,  TODAY - timedelta(days=3)),
]
for cat, desc, amt, exp_date in expenses:
    db.add(Expense(
        doctor_id=DOCTOR_ID, amount=amt, category=cat,
        description=desc, expense_date=exp_date, created_by=DOCTOR_ID,
    ))

db.commit()
print("  ✓ appointments + visits + bills + expenses seeded")

# ── Summary ───────────────────────────────────────────────────────────────────
from database.models import VisitStatus as VS, AppointmentStatus as AS
visits = db.query(Visit).filter(Visit.doctor_id == DOCTOR_ID, Visit.visit_date == TODAY).all()
appts  = db.query(Appointment).filter(Appointment.doctor_id == DOCTOR_ID,
                                       Appointment.appointment_date == TODAY).all()
bills  = db.query(Bill).filter(Bill.doctor_id == DOCTOR_ID).all()

print()
print("─" * 50)
print("DEMO SEED COMPLETE — Ananya Mehta")
print("─" * 50)
print(f"  Patients       : {len(patients) + 1}")
print(f"  Appointments   : {len(appts)} (today)")
print(f"  Visits/Queue   : {len(visits)}")
print(f"    Done         : {sum(1 for v in visits if v.status == VS.done)}")
print(f"    Billing pend : {sum(1 for v in visits if v.status == VS.billing_pending)}")
print(f"    Serving      : {sum(1 for v in visits if v.status == VS.serving)}")
print(f"    Waiting      : {sum(1 for v in visits if v.status == VS.waiting)}")
print(f"  Bills          : {len(bills)}")
total_rev = sum(b.paid_amount or 0 for b in bills)
print(f"  Revenue today  : ₹{total_rev:,.0f}")
print(f"  Expenses       : {len(expenses)} entries")
print("─" * 50)
db.close()
