from datetime import date, time, datetime, timedelta
from typing import List, Tuple
from sqlalchemy.orm import Session

from database.models import (
    Appointment, AppointmentStatus, BookedBy, DoctorSchedule, BlockedDate, BlockedTime,
    Patient, ReferralSource,
)


def _generate_slots(start: time, end: time, duration_mins: int) -> List[time]:
    """Generate all time slots between start and end with given duration."""
    slots = []
    current = datetime.combine(date.today(), start)
    end_dt = datetime.combine(date.today(), end)
    delta = timedelta(minutes=duration_mins)
    while current + delta <= end_dt:
        slots.append(current.time())
        current += delta
    return slots


def get_available_slots(
    doctor_id: int,
    appt_date: date,
    db: Session,
    filter_past: bool = True,
) -> List[str]:
    """Return list of available HH:MM time slots for a doctor on a given date.
    Merges slots across all active shifts for the day (supports multi-shift days).

    filter_past=True  — public booking: hide slots before current time for today.
    filter_past=False — doctor/staff booking: show ALL slots for any date so past
                        appointments can be recorded and pre-clinic bookings work.
    """
    # Check blocked date
    blocked = db.query(BlockedDate).filter(
        BlockedDate.doctor_id == doctor_id,
        BlockedDate.blocked_date == appt_date,
    ).first()
    if blocked:
        return []

    # Get ALL active schedules for that day (ordered by start time)
    dow = appt_date.weekday()
    schedules = (
        db.query(DoctorSchedule)
        .filter(
            DoctorSchedule.doctor_id == doctor_id,
            DoctorSchedule.day_of_week == dow,
            DoctorSchedule.is_active == True,
        )
        .order_by(DoctorSchedule.start_time)
        .all()
    )
    if not schedules:
        return []

    # Merge slots from all shifts (deduplicated + sorted)
    all_slots_set: set = set()
    for s in schedules:
        all_slots_set.update(_generate_slots(s.start_time, s.end_time, s.slot_duration))
    all_slots = sorted(all_slots_set)

    # Already-booked (non-cancelled) appointments
    booked = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == appt_date,
        Appointment.status != AppointmentStatus.cancelled,
    ).all()

    # Use highest max_patients across shifts as the daily cap.
    # Reserve walk_in_buffer slots so on-site walk-ins / emergencies always have room.
    total_max   = max(s.max_patients   for s in schedules)
    walk_buffer = max(s.walk_in_buffer for s in schedules)
    # Pre-bookable quota: subtract buffer, but also subtract walk-ins already added today
    walkins_today = sum(1 for a in booked if a.booked_by == BookedBy.walk_in or a.is_emergency)
    effective_max = max(0, total_max - max(walk_buffer, walkins_today))
    # Count only pre-booked appointments against effective_max
    prebooked_count = sum(1 for a in booked
                          if a.booked_by != BookedBy.walk_in and not a.is_emergency)
    if prebooked_count >= effective_max:
        return []

    booked_times = {a.appointment_time for a in booked}

    # Blocked time ranges for this date (e.g. 2–3 PM for a meeting/emergency)
    blocked_ranges = db.query(BlockedTime).filter(
        BlockedTime.doctor_id == doctor_id,
        BlockedTime.blocked_date == appt_date,
    ).all()

    def _in_blocked_range(slot: time) -> bool:
        for br in blocked_ranges:
            if br.start_time <= slot < br.end_time:
                return True
        return False

    # For public booking on today: hide slots that have already passed.
    # For doctor/staff booking: show every available slot regardless of current time
    # so they can (a) book pre-opening appointments and (b) record past visits.
    if filter_past and appt_date == date.today():
        now_time = datetime.now().time()
        available = [
            s for s in all_slots
            if s not in booked_times and s >= now_time and not _in_blocked_range(s)
        ]
    else:
        available = [
            s for s in all_slots
            if s not in booked_times and not _in_blocked_range(s)
        ]

    return [s.strftime("%H:%M") for s in available]


def is_slot_available(
    doctor_id: int, appt_date: date, appt_time: time, db: Session
) -> Tuple[bool, str]:
    """Returns (True, '') if slot is available, or (False, reason) otherwise.
    Checks all active shifts for the day.
    """
    blocked = db.query(BlockedDate).filter(
        BlockedDate.doctor_id == doctor_id,
        BlockedDate.blocked_date == appt_date,
    ).first()
    if blocked:
        return False, "This date is blocked. Please choose another date."

    dow = appt_date.weekday()
    schedules = (
        db.query(DoctorSchedule)
        .filter(
            DoctorSchedule.doctor_id == doctor_id,
            DoctorSchedule.day_of_week == dow,
            DoctorSchedule.is_active == True,
        )
        .order_by(DoctorSchedule.start_time)
        .all()
    )
    if not schedules:
        return False, "No working hours set for this day. Check Settings → Schedule."

    # Time must fall within at least one shift
    in_shift = any(s.start_time <= appt_time < s.end_time for s in schedules)
    if not in_shift:
        shifts_str = " / ".join(
            f"{s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}"
            for s in schedules
        )
        return False, f"Time is outside working hours ({shifts_str})."

    # Double-booking check
    conflict = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == appt_date,
        Appointment.appointment_time == appt_time,
        Appointment.status != AppointmentStatus.cancelled,
    ).first()
    if conflict:
        return False, "This time slot is already booked."

    # Max-patients cap (use highest across shifts), respecting walk_in_buffer
    total_max   = max(s.max_patients   for s in schedules)
    walk_buffer = max(s.walk_in_buffer for s in schedules)
    # Count all booked (walk-in + regular) to compute slots already used by walk-ins
    all_booked = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == appt_date,
        Appointment.status != AppointmentStatus.cancelled,
    ).all()
    walkins_today = sum(1 for a in all_booked if a.booked_by == BookedBy.walk_in or a.is_emergency)
    effective_max = max(0, total_max - max(walk_buffer, walkins_today))
    prebooked_count = sum(1 for a in all_booked
                          if a.booked_by != BookedBy.walk_in and not a.is_emergency)
    if prebooked_count >= effective_max:
        return False, "Maximum patients for this day has been reached."

    return True, ""


def is_slot_available_for_edit(
    doctor_id: int, appt_date: date, appt_time: time,
    exclude_appt_id: int, db: Session
) -> Tuple[bool, str]:
    """Same as is_slot_available but ignores the appointment being edited."""
    blocked = db.query(BlockedDate).filter(
        BlockedDate.doctor_id == doctor_id,
        BlockedDate.blocked_date == appt_date,
    ).first()
    if blocked:
        return False, "This date is blocked. Please choose another date."

    dow = appt_date.weekday()
    schedules = (
        db.query(DoctorSchedule)
        .filter(
            DoctorSchedule.doctor_id == doctor_id,
            DoctorSchedule.day_of_week == dow,
            DoctorSchedule.is_active == True,
        )
        .order_by(DoctorSchedule.start_time)
        .all()
    )
    if not schedules:
        return False, "No working hours set for this day. Check Settings → Schedule."

    in_shift = any(s.start_time <= appt_time < s.end_time for s in schedules)
    if not in_shift:
        shifts_str = " / ".join(
            f"{s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}"
            for s in schedules
        )
        return False, f"Time is outside working hours ({shifts_str})."

    conflict = db.query(Appointment).filter(
        Appointment.id != exclude_appt_id,
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == appt_date,
        Appointment.appointment_time == appt_time,
        Appointment.status != AppointmentStatus.cancelled,
    ).first()
    if conflict:
        return False, "This time slot is already booked."

    total_max   = max(s.max_patients   for s in schedules)
    walk_buffer = max(s.walk_in_buffer for s in schedules)
    all_booked = db.query(Appointment).filter(
        Appointment.id != exclude_appt_id,
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == appt_date,
        Appointment.status != AppointmentStatus.cancelled,
    ).all()
    walkins_today = sum(1 for a in all_booked if a.booked_by == BookedBy.walk_in or a.is_emergency)
    effective_max = max(0, total_max - max(walk_buffer, walkins_today))
    prebooked_count = sum(1 for a in all_booked
                          if a.booked_by != BookedBy.walk_in and not a.is_emergency)
    if prebooked_count >= effective_max:
        return False, "Maximum patients for this day has been reached."

    return True, ""


def has_open_appointment_on_date(
    doctor_id: int, phone: str, appt_date: date, db: Session,
    exclude_appt_id: int = 0,
) -> bool:
    """Return True if the patient (by phone) already has a scheduled appointment
    with this doctor on this date. Cancelled / completed / no-show are ignored."""
    patient = db.query(Patient).filter(
        Patient.doctor_id == doctor_id,
        Patient.phone == phone,
    ).first()
    if not patient:
        return False
    q = db.query(Appointment).filter(
        Appointment.doctor_id  == doctor_id,
        Appointment.patient_id == patient.id,
        Appointment.appointment_date == appt_date,
        Appointment.status == AppointmentStatus.scheduled,
    )
    if exclude_appt_id:
        q = q.filter(Appointment.id != exclude_appt_id)
    return q.first() is not None


def _title_name(name: str) -> str:
    """Trim whitespace and title-case each word (e.g. 'dr. john doe' → 'Dr. John Doe')."""
    return " ".join(w.capitalize() for w in name.strip().split())


def get_or_create_patient(
    doctor_id: int, name: str, phone: str, db: Session,
    age: int | None = None, gender: str | None = None,
    referral_source: str | None = None,
    referral_source_other: str | None = None,
) -> Patient:
    """Look up patient by phone for this doctor, or create a new record.

    If age/gender are provided they are always written (update existing too).

    `referral_source` is first-touch only — it is applied **only if the patient
    has no source yet** so we never overwrite a known origin on return visits.
    Explicit edits from the patient profile bypass this helper and may override.
    """
    name = _title_name(name)
    patient = db.query(Patient).filter(
        Patient.doctor_id == doctor_id,
        Patient.phone == phone,
    ).first()
    if not patient:
        patient = Patient(doctor_id=doctor_id, name=name, phone=phone)
        db.add(patient)
        db.flush()  # populate patient.id without full commit
    if age is not None:
        patient.age = age
    if gender:
        patient.gender = gender

    # First-touch attribution: write source only if unset
    if referral_source and patient.referral_source is None:
        try:
            patient.referral_source = ReferralSource(referral_source)
            if referral_source == ReferralSource.other.value and referral_source_other:
                # Trim and cap to the column length (120)
                patient.referral_source_other = referral_source_other.strip()[:120]
        except ValueError:
            # Unknown source value submitted — silently ignore so we never block a booking
            pass

    return patient
