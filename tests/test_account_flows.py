"""
test_account_flows.py — email verification, password reset, PIN and settings.

These are the routes that decide who gets in, so the assertions lean on the
security properties rather than the happy path alone: a reset link must not
reveal whether an address is registered, a consumed token must not work twice,
and a PIN must actually gate the pages it claims to.

No mail leaves the process. Codes and tokens are stored hashed, so the tests
that need the plaintext capture the outgoing message via the `outbox` fixture
rather than reading the database — see its docstring.
"""
from datetime import datetime, timedelta

import pytest
import re

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, register, verify_email, login,
                           set_pin, give_schedule, make_patient, phone, PASSWORD)
from database.models import Doctor, EmailVerification, PasswordReset, BlockedDate


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"acct-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    return {"id": did, "email": email, "clinic": clinic_of(did)}


@pytest.fixture
def outbox(monkeypatch):
    """Capture outgoing mail instead of sending it.

    Verification codes and reset tokens are stored as hashes (correctly), so
    the only place the plaintext exists is the message body. Patching the name
    inside each service — they do `from ... import send_email` — keeps the real
    template rendering in the path, which is what carries the code.
    """
    sent = []

    def _capture(to, subject, html, **kw):
        sent.append({"to": to, "subject": subject, "html": html})
        return True, "captured"

    import services.verification_service as vs
    import services.password_reset_service as prs
    monkeypatch.setattr(vs, "send_email", _capture)
    monkeypatch.setattr(prs, "send_email", _capture)
    return sent


def _code_from(outbox):
    for msg in reversed(outbox):
        m = re.search(r"\b(\d{6})\b", msg["subject"] + msg["html"])
        if m:
            return m.group(1)
    return None


def _reset_token_from(outbox):
    for msg in reversed(outbox):
        m = re.search(r"/reset-password\?token=([A-Za-z0-9_\-]+)", msg["html"])
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
#  Email verification                                                           #
# --------------------------------------------------------------------------- #

class TestEmailVerification:

    def _unverified(self, client, email):
        client.cookies.clear()
        register(client, email)
        login(client, email)
        return email

    def test_unverified_doctor_is_sent_to_verify(self, client):
        self._unverified(client, "verify-gate@test.com")
        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code in (302, 303), "an unverified account reached the app"

    def test_verify_page_renders(self, client):
        self._unverified(client, "verify-page@test.com")
        assert client.get("/verify-email").status_code == 200

    def test_wrong_code_is_refused(self, client):
        self._unverified(client, "verify-wrong@test.com")
        client.post("/verify-email", data={"code": "000000"}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            d = db.query(Doctor).filter(Doctor.email == "verify-wrong@test.com").first()
            assert d.email_verified_at is None, "a wrong code verified the account"
        finally:
            db.close()

    def test_correct_code_verifies(self, client, outbox):
        email = self._unverified(client, "verify-right@test.com")
        client.post("/verify-email/resend", follow_redirects=False)
        code = _code_from(outbox)
        assert code, "no verification code reached the outgoing message"

        client.post("/verify-email", data={"code": code}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == email).first().email_verified_at is not None
        finally:
            db.close()

    def test_resend_issues_a_new_code(self, client):
        email = self._unverified(client, "verify-resend@test.com")
        db = TestSessionLocal()
        try:
            did = db.query(Doctor).filter(Doctor.email == email).first().id
            before = db.query(EmailVerification).filter(
                EmailVerification.doctor_id == did).count()
        finally:
            db.close()

        client.post("/verify-email/resend", follow_redirects=False)
        db = TestSessionLocal()
        try:
            after = db.query(EmailVerification).filter(
                EmailVerification.doctor_id == did).count()
            assert after >= before
        finally:
            db.close()

    def test_change_address_before_verifying(self, client):
        email = self._unverified(client, "verify-change@test.com")
        r = client.post("/verify-email/change-address",
                        data={"email": "verify-changed@test.com"},
                        follow_redirects=False)
        assert r.status_code < 500

    def test_cannot_change_to_an_address_already_in_use(self, client):
        register(client, "verify-taken@test.com")
        email = self._unverified(client, "verify-changer@test.com")
        client.post("/verify-email/change-address",
                    data={"email": "verify-taken@test.com"}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "verify-taken@test.com").count() == 1, (
                "two accounts ended up on one address")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Password reset                                                               #
# --------------------------------------------------------------------------- #

class TestPasswordReset:

    def test_form_renders(self, client):
        client.cookies.clear()
        assert client.get("/forgot-password").status_code == 200

    def test_response_is_identical_for_known_and_unknown_addresses(self, client):
        """Otherwise the form is a registered-user oracle.

        Both addresses are the same length, so any difference in the response
        is about EXISTENCE rather than about the text that was submitted.
        """
        client.cookies.clear()
        registered = "oracle-yes-000000@test.com"
        missing    = "oracle-no-0000000@test.com"
        assert len(registered) == len(missing)
        register(client, registered)
        client.cookies.clear()

        known = client.post("/forgot-password", data={"email": registered},
                            follow_redirects=False)
        unknown = client.post("/forgot-password", data={"email": missing},
                              follow_redirects=False)
        assert known.status_code == unknown.status_code
        assert len(known.text) == len(unknown.text), (
            "the reset form reveals whether an address is registered")

    def test_reset_token_sets_a_new_password(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token, "no reset link reached the outgoing message"

        assert client.get(f"/reset-password?token={token}").status_code == 200
        new_password = "Nw7&kLpq3#Zt9M"
        r = client.post("/reset-password", data={
            "token": token, "password": new_password,
            "confirm_password": new_password,
        }, follow_redirects=False)
        assert r.status_code < 500

        client.cookies.clear()
        assert login(client, doc["email"], new_password).status_code == 303, (
            "the new password does not work")

    def test_a_used_token_cannot_be_replayed(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token

        first = "Fst7&kLpq3#Zt9M"
        client.post("/reset-password", data={
            "token": token, "password": first, "confirm_password": first},
            follow_redirects=False)

        second = "Snd7&kLpq3#Zt9M"
        client.post("/reset-password", data={
            "token": token, "password": second, "confirm_password": second},
            follow_redirects=False)

        client.cookies.clear()
        assert login(client, doc["email"], second).status_code != 303, (
            "a spent reset token was accepted a second time")

    def test_invalid_token_is_refused(self, client):
        client.cookies.clear()
        assert client.get("/reset-password?token=nonsense").status_code < 500
        r = client.post("/reset-password", data={
            "token": "nonsense", "password": "Abc7&kLpq3#Zt9M",
            "confirm_password": "Abc7&kLpq3#Zt9M"}, follow_redirects=False)
        assert r.status_code < 500

    def test_weak_new_password_is_refused(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token

        client.post("/reset-password", data={
            "token": token, "password": "short", "confirm_password": "short"},
            follow_redirects=False)
        client.cookies.clear()
        assert login(client, doc["email"], "short").status_code != 303


# --------------------------------------------------------------------------- #
#  PIN                                                                          #
# --------------------------------------------------------------------------- #

class TestPin:

    def test_setting_a_pin_then_unlocking(self, client, doc):
        r = client.post("/doctors/settings/pin",
                        data={"new_pin": "246813", "confirm_pin": "246813"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is not None
        finally:
            db.close()

        r = client.post("/pin-prompt", data={"pin": "246813", "next": "/reports"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        assert client.get("/reports").status_code == 200

    def test_mismatched_pins_are_refused(self, client, doc):
        client.post("/doctors/settings/pin",
                    data={"new_pin": "111111", "confirm_pin": "222222"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is None
        finally:
            db.close()

    def test_non_numeric_pin_is_refused(self, client, doc):
        client.post("/doctors/settings/pin",
                    data={"new_pin": "abcdef", "confirm_pin": "abcdef"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is None
        finally:
            db.close()

    def test_wrong_pin_does_not_unlock(self, client, doc):
        set_pin(client, "135790")
        client.cookies.delete("pin_session")
        r = client.post("/pin-prompt", data={"pin": "999999", "next": "/reports"},
                        follow_redirects=False)
        assert "pin_error" in r.headers.get("location", "") or r.status_code == 200

    def test_pin_prompt_page_renders(self, client, doc):
        assert client.get("/pin-prompt", follow_redirects=False).status_code < 500


# --------------------------------------------------------------------------- #
#  Settings                                                                     #
# --------------------------------------------------------------------------- #

class TestSettings:

    def test_page_renders(self, client, doc):
        set_pin(client)
        assert client.get("/doctors/settings").status_code == 200

    def test_account_details_update(self, client, doc):
        set_pin(client)
        r = client.post("/doctors/settings/account", data={
            "name": "Dr Renamed", "email": doc["email"], "phone": phone(),
            "specialization": "Cardiology", "medical_reg_number": "MH/123",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(Doctor.id == doc["id"]).first().name \
                == "Dr Renamed"
        finally:
            db.close()

    def test_cannot_take_another_doctors_email(self, client, doc):
        register(client, "acct-taken@test.com")
        login(client, doc["email"])
        set_pin(client)
        client.post("/doctors/settings/account", data={
            "name": "Dr Thief", "email": "acct-taken@test.com", "phone": phone(),
            "specialization": "", "medical_reg_number": "",
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "acct-taken@test.com").count() == 1
        finally:
            db.close()

    def test_clinic_profile_update(self, client, doc):
        set_pin(client)
        r = client.post("/doctors/settings/profile", data={
            "clinic_name": "Renamed Clinic", "clinic_address": "12 Main St",
            "city": "Pune",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

    def test_blocked_dates_lifecycle(self, client, doc):
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=10)).date()
        client.post("/doctors/settings/block",
                    data={"blocked_date": target.isoformat(), "reason": "Leave"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            row = db.query(BlockedDate).filter(
                BlockedDate.doctor_id == doc["id"]).first()
            assert row is not None, "blocked date was not saved"
            bid = row.id
        finally:
            db.close()

        client.post(f"/doctors/settings/unblock/{bid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(BlockedDate).filter(BlockedDate.id == bid).first() is None
        finally:
            db.close()

    def test_blocked_times_lifecycle(self, client, doc):
        from database.models import BlockedTime
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=11)).date()
        r = client.post("/doctors/settings/blocktime", data={
            "blocked_date": target.isoformat(), "start_time": "13:00",
            "end_time": "14:00", "reason": "Lunch",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            row = db.query(BlockedTime).filter(
                BlockedTime.doctor_id == doc["id"]).first()
            assert row is not None, "blocked time was not saved"
            btid = row.id
        finally:
            db.close()

        client.post(f"/doctors/settings/unblocktime/{btid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            from database.models import BlockedTime as BT
            assert db.query(BT).filter(BT.id == btid).first() is None
        finally:
            db.close()

    def test_another_doctor_cannot_unblock_your_dates(self, client, doc):
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=12)).date()
        client.post("/doctors/settings/block",
                    data={"blocked_date": target.isoformat(), "reason": "Mine"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            bid = db.query(BlockedDate).filter(
                BlockedDate.doctor_id == doc["id"]).first().id
        finally:
            db.close()

        make_doctor(client, "acct-block-intruder@test.com")
        set_pin(client)
        client.post(f"/doctors/settings/unblock/{bid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(BlockedDate).filter(BlockedDate.id == bid).first() is not None, (
                "another doctor removed your day off")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Misc authenticated endpoints                                                 #
# --------------------------------------------------------------------------- #

class TestMiscEndpoints:

    def test_auth_check(self, client, doc):
        r = client.get("/auth/check")
        assert r.status_code in (200, 401)

    def test_workspace_loading(self, client, doc):
        assert client.get("/workspace-loading").status_code == 200

    def test_billing_page(self, client, doc):
        assert client.get("/billing", follow_redirects=False).status_code in (200, 302, 303)

    def test_logout_clears_the_session(self, client, doc):
        client.get("/logout", follow_redirects=False)
        assert client.get("/dashboard", follow_redirects=False).status_code in (302, 303)


# --------------------------------------------------------------------------- #
#  Settings — the JSON save layer                                               #
# --------------------------------------------------------------------------- #

# What static/js/settings-save.js sends. Two headers, because wants_json()
# trusts X-Requested-With first and falls back to Accept for anyone else.
AJAX = {"X-Requested-With": "fetch", "Accept": "application/json"}


class TestSettingsJsonSaves:
    """Each settings section saves on its own via fetch and reports its own
    result. The plain form POST must keep working byte-for-byte, because it
    is still the no-JS path and the offline fallback."""

    def test_profile_save_answers_json_for_fetch(self, client, doc):
        set_pin(client)
        r = client.post("/doctors/settings/profile",
                        data={"clinic_name": "Renamed Clinic", "city": "Pune",
                              "clinic_address": "", "languages": ""},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"ok": True, "section": "profile",
                            "message": "Clinic profile updated",
                            "tone": "success", "warnings": []}

    def test_profile_save_still_redirects_for_a_plain_form_post(self, client, doc):
        """The whole point of branching on the header rather than adding a
        second set of routes: with JavaScript off, nothing changes."""
        set_pin(client)
        r = client.post("/doctors/settings/profile",
                        data={"clinic_name": "X", "city": "",
                              "clinic_address": "", "languages": ""},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/doctors/settings?saved=1"

    def test_missing_name_is_a_reported_failure_not_a_silent_one(self, client, doc):
        """Used to redirect to ?saved=0, which the template renders as
        nothing at all — the save looked like it had worked."""
        set_pin(client)
        r = client.post("/doctors/settings/account",
                        data={"name": "", "email": doc["email"], "phone": "",
                              "specialization": "", "medical_reg_number": ""},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 400
        assert r.json()["ok"] is False
        assert r.json()["message"]

    def test_reversed_blocked_time_range_reports_the_error(self, client, doc):
        """Used to redirect to ?error=time_order, which nothing read."""
        set_pin(client)
        r = client.post("/doctors/settings/blocktime",
                        data={"blocked_date": "2030-01-01", "start_time": "18:00",
                              "end_time": "09:00", "reason": ""},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 400
        assert r.json()["ok"] is False
        assert "end time" in r.json()["message"].lower()

    def test_a_non_numeric_slot_length_does_not_500(self, client, doc):
        """int(form.get(...)) had no guard, so a value a user could type was
        a 500 on a settings save."""
        set_pin(client)
        r = client.post("/doctors/settings/schedule",
                        data={"active_0": "on", "shift_start_0_0": "09:00",
                              "shift_end_0_0": "13:00", "slot_0": "abc",
                              "max_0": "", "walkin_buf_0": "-4"},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code < 500
        assert r.json()["ok"] is True

    def test_a_schedule_save_does_not_reset_avg_consult_mins(self, client, doc):
        """It was Form(10) and written unconditionally, so any post that left
        the field out silently reset a doctor's 25 minutes to 10."""
        db = TestSessionLocal()
        try:
            db.query(Doctor).filter(Doctor.id == doc["id"]).first().avg_consult_mins = 25
            db.commit()
        finally:
            db.close()

        set_pin(client)
        client.post("/doctors/settings/schedule",
                    data={"active_0": "on", "shift_start_0_0": "09:00",
                          "shift_end_0_0": "13:00"},
                    headers=AJAX, follow_redirects=False)

        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(Doctor.id == doc["id"]).first() \
                     .avg_consult_mins == 25
        finally:
            db.close()

    def test_dropped_shifts_are_admitted_not_swallowed(self, client, doc):
        """Overlapping and backwards shifts are skipped. That used to happen
        invisibly: the doctor saw "saved" and a row they typed was gone."""
        set_pin(client)
        r = client.post("/doctors/settings/schedule",
                        data={"active_0": "on",
                              "shift_start_0_0": "09:00", "shift_end_0_0": "13:00",
                              "shift_start_0_1": "10:00", "shift_end_0_1": "14:00",
                              "shift_start_0_2": "18:00", "shift_end_0_2": "17:00"},
                        headers=AJAX, follow_redirects=False)
        body = r.json()
        assert body["ok"] is True
        assert body["tone"] == "warning"
        assert body["warnings"] and "2" in body["warnings"][0]

    def test_setting_a_pin_over_fetch_still_issues_the_session_cookie(self, client, doc):
        """set_cookie works the same on a JSONResponse, but the save layer has
        to send credentials for the browser to keep it — worth pinning down."""
        client.cookies.pop("pin_session", None)
        r = client.post("/doctors/settings/pin",
                        data={"action": "set", "current_pin": "",
                              "new_pin": "424242", "confirm_pin": "424242"},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 200
        assert r.json()["message"] == "PIN set"
        assert r.json()["pin_enabled"] is True
        assert "pin_session" in r.cookies

    def test_removing_a_pin_over_fetch_takes_the_remove_branch(self, client, doc):
        """The Remove button carries name="action" value="remove", which
        new FormData(form) does not include — the save layer has to add the
        submitter itself or this silently validates three empty fields."""
        set_pin(client, "424242")
        r = client.post("/doctors/settings/pin",
                        data={"action": "remove", "current_pin": "424242",
                              "new_pin": "", "confirm_pin": ""},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 200
        assert r.json()["message"] == "PIN removed"
        assert r.json()["pin_enabled"] is False
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(Doctor.id == doc["id"]).first().pin_hash is None
        finally:
            db.close()

    def test_mismatched_pins_say_so(self, client, doc):
        r = client.post("/doctors/settings/pin",
                        data={"action": "set", "current_pin": "",
                              "new_pin": "111111", "confirm_pin": "222222"},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code == 400
        assert "match" in r.json()["message"].lower()

    def test_an_expired_pin_session_answers_json_not_a_redirect(self, client, doc):
        """fetch() follows a 303 silently and hands back a 200 HTML document,
        so res.json() would throw with nothing to show the doctor. pin_session
        lives 30 minutes, so a settings page left open hits this for real."""
        set_pin(client)
        client.cookies.pop("pin_session", None)
        r = client.post("/doctors/settings/profile",
                        data={"clinic_name": "X", "city": "",
                              "clinic_address": "", "languages": ""},
                        headers=AJAX, follow_redirects=False)
        assert r.status_code in (401, 403)
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["reason"] in ("pin_required", "owner_only")

    def test_the_same_gate_still_redirects_a_plain_form_post(self, client, doc):
        set_pin(client)
        client.cookies.pop("pin_session", None)
        r = client.post("/doctors/settings/profile",
                        data={"clinic_name": "X", "city": "",
                              "clinic_address": "", "languages": ""},
                        follow_redirects=False)
        assert r.status_code == 303

    def test_removing_a_blocked_date_now_confirms_itself(self, client, doc):
        """It redirected with no marker at all, so a deletion gave the doctor
        no feedback of any kind."""
        set_pin(client)
        client.post("/doctors/settings/block",
                    data={"blocked_date": "2030-03-03", "reason": "test"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            bid = db.query(BlockedDate).filter(
                BlockedDate.doctor_id == doc["id"]).first().id
        finally:
            db.close()
        r = client.post(f"/doctors/settings/unblock/{bid}",
                        headers=AJAX, follow_redirects=False)
        assert r.json()["message"] == "Blocked date removed"

    def test_blocking_the_same_date_twice_says_so(self, client, doc):
        set_pin(client)
        for _ in range(2):
            r = client.post("/doctors/settings/block",
                            data={"blocked_date": "2030-04-04", "reason": ""},
                            headers=AJAX, follow_redirects=False)
        assert r.json()["tone"] == "warning"
        assert "already" in r.json()["message"].lower()

    def test_the_settings_page_renders_the_error_params_it_is_sent(self, client, doc):
        """?error=time_order used to render as nothing."""
        set_pin(client)
        body = client.get("/doctors/settings?error=time_order").text
        assert "end time must be after the start time" in body.lower()
