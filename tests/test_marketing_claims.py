"""
test_marketing_claims.py — the site must not promise what the app doesn't do.

WhatsApp notifications were dropped: Twilio was never configured in production
(placeholder credentials), so no reminder, confirmation or receipt has ever been
sent. The marketing copy still promised them on the login, register, pricing,
billing, invite and landing pages — the first thing a doctor reads, and the
first claim a pilot clinic would catch us out on.

These pin the removal. When WhatsApp notifications really ship, delete the
phrase from FORBIDDEN in the same commit that turns them on.

Deliberately NOT forbidden: "Share on WhatsApp" (opens the doctor's own
WhatsApp, works today), the patient's preferred-contact / consent fields (data
the doctor records), and the support link (a contact channel, not a feature).
"""
from datetime import datetime

import pytest

from tests.helpers import make_doctor, clinic_of, give_schedule

FORBIDDEN = (
    "whatsapp reminders",
    "whatsapp-first",
    "whatsapp alerts",
    "whatsapp confirmations",
    "whatsapp notifications",
)

PUBLIC = ["/", "/login", "/register", "/pricing"]


def _claims(html: str) -> list[str]:
    low = html.lower()
    return [p for p in FORBIDDEN if p in low]


@pytest.mark.parametrize("path", PUBLIC)
def test_public_pages_promise_no_whatsapp_notifications(client, path):
    client.cookies.clear()
    body = client.get(path, follow_redirects=True).text
    assert _claims(body) == [], f"{path} still claims: {_claims(body)}"


def test_signed_in_pages_promise_no_whatsapp_notifications(client):
    client.cookies.clear()
    email = f"claims-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    give_schedule(did, clinic_of(did))
    for path in ("/billing", "/appointments/new"):
        body = client.get(path, follow_redirects=True).text
        assert _claims(body) == [], f"{path} still claims: {_claims(body)}"


def test_no_template_carries_the_claim():
    """Covers templates no route above happens to render (e.g. the invite page)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "templates"
    offenders = [f"{f.relative_to(root)}: {c}"
                 for f in root.rglob("*.html") for c in _claims(f.read_text())]
    assert offenders == [], "templates still promise WhatsApp: " + ", ".join(offenders)
