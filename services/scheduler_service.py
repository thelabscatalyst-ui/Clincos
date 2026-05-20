"""
Background scheduler — APScheduler reminder jobs.

Runs every 15 minutes:
  • T-24h  — day-before reminder
  • T-2h   — same-day reminder
  • Auto no-show — marks appointments as no_show if >1 hour past scheduled time
    with no check-in recorded.

Starts on app startup (wired into main.py lifespan).
Gracefully skips if Twilio is not configured.
"""
import logging
from datetime import datetime, timedelta, date

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


# ------------------------------------------------------------------ #
#  Reminder job                                                        #
# ------------------------------------------------------------------ #

def _check_reminders():
    """Query upcoming scheduled appointments and fire pending reminders."""
    # Import here to avoid circular import at module load time
    from database.connection import SessionLocal
    from database.models import Appointment, AppointmentStatus
    from services.notification_service import notify_reminder

    db = SessionLocal()
    try:
        now = datetime.now()

        # ---- 24-hour window: appt is 23h–25h from now ----
        win_24h_lo = now + timedelta(hours=23)
        win_24h_hi = now + timedelta(hours=25)

        appts_24h = (
            db.query(Appointment)
            .filter(
                Appointment.status == AppointmentStatus.scheduled,
                Appointment.reminder_24h_sent == False,  # noqa: E712
            )
            .all()
        )
        for appt in appts_24h:
            appt_dt = datetime.combine(appt.appointment_date, appt.appointment_time)
            if win_24h_lo <= appt_dt <= win_24h_hi:
                _ = appt.doctor   # lazy-load
                notify_reminder(appt, appt.doctor, db, "24h")
                appt.reminder_24h_sent = True
                db.commit()
                logger.info(f"24h reminder fired: appt #{appt.id}")

        # ---- 2-hour window: appt is 1.5h–2.5h from now ----
        win_2h_lo = now + timedelta(minutes=90)
        win_2h_hi = now + timedelta(minutes=150)

        appts_2h = (
            db.query(Appointment)
            .filter(
                Appointment.status == AppointmentStatus.scheduled,
                Appointment.reminder_2h_sent == False,  # noqa: E712
            )
            .all()
        )
        for appt in appts_2h:
            appt_dt = datetime.combine(appt.appointment_date, appt.appointment_time)
            if win_2h_lo <= appt_dt <= win_2h_hi:
                _ = appt.doctor   # lazy-load
                notify_reminder(appt, appt.doctor, db, "2h")
                appt.reminder_2h_sent = True
                db.commit()
                logger.info(f"2h reminder fired: appt #{appt.id}")

    except Exception as exc:
        logger.error(f"Reminder check error: {exc}", exc_info=True)
    finally:
        db.close()


# ------------------------------------------------------------------ #
#  Auto no-show job                                                    #
# ------------------------------------------------------------------ #

def _auto_no_show():
    """Mark appointments as no_show when patient hasn't arrived > 1 hour after slot time."""
    from database.connection import SessionLocal
    from database.models import Appointment, AppointmentStatus, BookedBy, Visit, VisitStatus

    db = SessionLocal()
    try:
        now      = datetime.now()
        today    = date.today()
        cutoff   = now - timedelta(hours=1)   # appointments whose time < 1h ago

        # Candidates: scheduled appointments for today that haven't been checked in
        candidates = (
            db.query(Appointment)
            .filter(
                Appointment.appointment_date == today,
                Appointment.status           == AppointmentStatus.scheduled,
                Appointment.booked_by        != BookedBy.walk_in,   # walk-ins always auto check-in
            )
            .all()
        )

        marked = 0
        for appt in candidates:
            appt_dt = datetime.combine(appt.appointment_date, appt.appointment_time)
            if appt_dt > cutoff:
                continue   # not yet 1 hour overdue

            # Skip if a visit exists (patient checked in at some point)
            has_visit = (
                db.query(Visit)
                .filter(
                    Visit.appointment_id == appt.id,
                    Visit.status.notin_([VisitStatus.cancelled]),
                )
                .first()
            )
            if has_visit:
                continue

            appt.status = AppointmentStatus.no_show
            marked += 1

        if marked:
            db.commit()
            logger.info(f"Auto no-show: marked {marked} appointment(s).")

    except Exception as exc:
        logger.error(f"Auto no-show error: {exc}", exc_info=True)
    finally:
        db.close()


# ------------------------------------------------------------------ #
#  Start / stop (called from main.py lifespan)                        #
# ------------------------------------------------------------------ #

def start_scheduler():
    """Start the background reminder scheduler."""
    _scheduler.add_job(
        _check_reminders,
        trigger="interval",
        minutes=15,
        id="reminder_check",
        replace_existing=True,
        misfire_grace_time=120,   # allow up to 2 min late
    )
    _scheduler.add_job(
        _auto_no_show,
        trigger="interval",
        minutes=15,
        id="auto_no_show",
        replace_existing=True,
        misfire_grace_time=120,
    )
    _scheduler.start()
    logger.info("Reminder scheduler started (every 15 min).")


def stop_scheduler():
    """Stop the scheduler gracefully on app shutdown."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Reminder scheduler stopped.")
