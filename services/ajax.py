"""One place that decides whether a POST is answered with JSON or a redirect.

Every settings save route serves two callers: the fetch layer in
static/js/settings-save.js, and a plain browser form POST — which is still
what happens with JavaScript off, and what the offline retry in base.html
falls back to. Rather than duplicating that branch in eleven routes, they all
end in save_result().

The Accept check deliberately mirrors main.py's 401 handler so the app has one
convention for "this caller wants JSON", not two.
"""

from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


def wants_json(request: Request) -> bool:
    """True when the caller can read a JSON body instead of following a 303.

    X-Requested-With is the explicit signal our own JS sends and is the one to
    trust. Accept is the fallback, matching main.py's unauthorized_handler.

    A bare fetch() with no headers sends `Accept: */*`, which must NOT count —
    otherwise a plain browser navigation that happens to send */* would be
    handed JSON it cannot render.
    """
    if request.headers.get("x-requested-with", "").lower() == "fetch":
        return True
    return "application/json" in request.headers.get("accept", "")


def save_result(
    request: Request,
    *,
    ok: bool,
    message: str,
    redirect: str,
    section: str = "",
    tone: Optional[str] = None,
    warnings: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
) -> Response:
    """JSON for the fetch layer, or the historical 303 for a plain form POST.

    `tone` is deliberately separate from `ok` so a route can report "saved,
    but with caveats" — the schedule save uses it to admit which shifts it
    silently dropped instead of pretending everything landed.

    Returns a real Response object, never writes cookies itself, so callers
    that need to set or clear one (the PIN route) can still do so on the way
    out.
    """
    if not wants_json(request):
        return RedirectResponse(url=redirect, status_code=303)

    body: Dict[str, Any] = {
        "ok":       ok,
        "section":  section,
        "message":  message,
        "tone":     tone or ("success" if ok else "error"),
        "warnings": warnings or [],
    }
    if extra:
        body.update(extra)

    if status_code is None:
        status_code = 200 if ok else 400
    return JSONResponse(body, status_code=status_code)


def gate_json(reason: str, message: str, redirect: str, status_code: int) -> JSONResponse:
    """The JSON half of an auth/plan/PIN gate.

    These gates redirect for a normal page load, which is right — but fetch()
    follows redirects silently and hands back a 200 HTML document, so the
    caller's res.json() explodes with nothing useful to show the user. The
    fetch layer keys off `reason` to decide whether to reload or just warn.
    """
    return JSONResponse(
        {"ok": False, "reason": reason, "message": message, "redirect": redirect},
        status_code=status_code,
    )
