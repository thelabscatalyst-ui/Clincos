"""
storage_service.py — the patient document vault, backed by Cloudflare R2.

Why this exists
---------------
The app container on Railway has no persistent disk. The only volume in the
project is attached to Postgres, so anything written to `uploads/` lived until
the next deploy and then vanished. Rows in `patient_documents` and `note_files`
survived; the files they pointed at did not. A clinic uploading a lab report on
Monday would not find it on Friday.

Contract (mirrors services/email_service.send_email)
----------------------------------------------------
  * Nothing here raises. Every function returns a value that says what happened.
    A storage outage must degrade to "file missing", never to a 500 on a
    patient's record page.
  * "Not configured" is a normal state, not an error. With no R2_* settings the
    backend falls back to local disk under `uploads/`, using the same layout as
    before — so local development and the test suite need no credentials and no
    network.

Keys, not paths
---------------
An object key looks like `patients/{doctor_id}/{patient_id}/{stored_name}`.
Deliberately identical to the old on-disk layout, so the disk fallback reads and
writes exactly the files the previous code did, and the migration is a copy
rather than a re-organisation.

Access control is NOT here
--------------------------
This module answers "give me the bytes for this key" and nothing more. Every
caller is responsible for having already proved the requesting doctor owns the
row. Files stay behind the authenticated routes in routers/patients.py — there
are no public objects and no presigned URLs, because a presigned URL is a link
that works for whoever holds it, which is the opposite of what a medical record
needs.
"""
import logging
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Local fallback root. Matches the historical layout so existing files are
# found unchanged when R2 is not configured.
_DISK_ROOT = Path("uploads")

_client = None          # lazily built boto3 client, cached
_client_failed = False   # don't retry a broken client on every request


# --------------------------------------------------------------------------- #
#  Configuration                                                                #
# --------------------------------------------------------------------------- #

def is_configured() -> bool:
    """True when R2 credentials are present. False means the disk fallback."""
    return bool(
        settings.R2_ACCOUNT_ID
        and settings.R2_ACCESS_KEY_ID
        and settings.R2_SECRET_ACCESS_KEY
        and settings.R2_BUCKET
    )


def backend_name() -> str:
    """For logs and the diagnose script — which backend is actually live."""
    return "r2" if is_configured() else "disk"


def _r2():
    """The boto3 client, or None if it cannot be built.

    Cached, including the failure: a bad endpoint or missing boto3 should be
    logged once, not once per uploaded file.

    A None return is NOT a cue to use the disk. See _r2_required().
    """
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            # R2 speaks S3, but not the newer default checksum headers boto3
            # started sending in 2025 — they make every PUT fail with
            # "Unsupported header". Pin the classic behaviour.
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return _client
    except Exception as exc:                      # pragma: no cover - env dependent
        _client_failed = True
        logger.error("R2 client could NOT be built (%s: %s) — the vault is "
                     "unavailable; refusing to write to local disk instead",
                     type(exc).__name__, exc)
        return None


class _VaultUnavailable(Exception):
    """R2 is configured but unreachable. Raised internally, never escapes."""


def _r2_required():
    """The client when R2 is configured, raising if it cannot be built.

    This exists because of a bug caught in verification. The first version of
    this module treated "configured but no client" the same as "not
    configured", so a missing boto3 made uploads fall through to the local
    disk. Every screen said success — and on Railway that disk is destroyed by
    the next deploy. Silently writing patient files somewhere doomed is the
    exact failure this whole module was written to remove, and it was invisible
    because it looked identical to working.

    So: once R2 is configured, disk is never a fallback. A configured vault
    that cannot be reached fails loudly and the caller declines to create a
    row, leaving the doctor with a visible error instead of a file that
    evaporates a day later.
    """
    client = _r2()
    if client is None:
        raise _VaultUnavailable("R2 is configured but the client is unavailable")
    return client


# --------------------------------------------------------------------------- #
#  Keys                                                                         #
# --------------------------------------------------------------------------- #

def object_key(doctor_id: int, patient_id: int, stored_name: str) -> str:
    """The key for one patient file."""
    return f"{patient_prefix(doctor_id, patient_id)}{stored_name}"


def patient_prefix(doctor_id: int, patient_id: int) -> str:
    """Everything belonging to one patient. Trailing slash matters — without it
    `patients/1/2` would also match `patients/1/23`."""
    return f"patients/{doctor_id}/{patient_id}/"


def _safe_key(key: str) -> Optional[str]:
    """Reject anything that could climb out of its prefix.

    `stored_name` is already run through _safe_filename() on the way in, so this
    is the second line rather than the first. It matters because a key is not a
    path but is used as one by the disk backend, and because a malformed key
    reaching R2 would silently create an object somewhere unexpected.
    """
    k = (key or "").strip().lstrip("/")
    if not k or ".." in k.split("/") or "\x00" in k or "\\" in k:
        logger.warning("rejected unsafe object key: %r", key)
        return None
    return k


def _disk_path(key: str) -> Optional[Path]:
    """Map a key onto the local fallback tree, refusing to escape the root."""
    safe = _safe_key(key)
    if safe is None:
        return None
    path = (_DISK_ROOT / safe).resolve()
    if not str(path).startswith(str(_DISK_ROOT.resolve())):
        logger.warning("object key escaped the upload root: %r", key)
        return None
    return path


# --------------------------------------------------------------------------- #
#  Operations                                                                   #
# --------------------------------------------------------------------------- #

def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> tuple[bool, str]:
    """Store bytes under `key`. Returns (ok, detail). Never raises."""
    safe = _safe_key(key)
    if safe is None:
        return False, "unsafe key"

    if is_configured():
        # No disk fallback past this point, for any reason. A write that lands
        # on the container filesystem looks like success and then disappears at
        # the next deploy.
        try:
            _r2_required().put_object(
                Bucket=settings.R2_BUCKET,
                Key=safe,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )
            return True, "r2"
        except _VaultUnavailable:
            return False, "vault unavailable"
        except Exception as exc:
            logger.error("R2 put failed for %s (%s: %s)", safe, type(exc).__name__, exc)
            return False, f"r2 error: {type(exc).__name__}"

    path = _disk_path(safe)
    if path is None:
        return False, "unsafe key"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True, "disk"
    except Exception as exc:
        logger.error("disk put failed for %s (%s: %s)", safe, type(exc).__name__, exc)
        return False, f"disk error: {type(exc).__name__}"


def get(key: str) -> Optional[bytes]:
    """The bytes for `key`, or None if it is missing or unreadable.

    Returns bytes rather than a stream because uploads are capped at
    MAX_FILE_BYTES (10 MB) in routers/patients.py. If that cap ever rises
    meaningfully this should become a streaming read.
    """
    safe = _safe_key(key)
    if safe is None:
        return None

    if is_configured():
        try:
            obj = _r2_required().get_object(Bucket=settings.R2_BUCKET, Key=safe)
            return obj["Body"].read()
        except _VaultUnavailable:
            return None
        except Exception as exc:
            # A missing object is ordinary (deleted, or lost to an old deploy);
            # anything else is worth a log line but reads the same to the
            # caller. Reading from disk here would be worse than useless: it
            # would serve one container's leftovers as if they were the vault.
            if type(exc).__name__ not in ("NoSuchKey", "ClientError"):
                logger.error("R2 get failed for %s (%s: %s)", safe, type(exc).__name__, exc)
            else:
                logger.info("R2 object not found: %s", safe)
            return None

    path = _disk_path(safe)
    if path is None or not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception as exc:
        logger.error("disk get failed for %s (%s: %s)", safe, type(exc).__name__, exc)
        return None


def exists(key: str) -> bool:
    """Whether an object is present. Used by the migration report."""
    safe = _safe_key(key)
    if safe is None:
        return False

    if is_configured():
        try:
            _r2_required().head_object(Bucket=settings.R2_BUCKET, Key=safe)
            return True
        except Exception:
            return False

    path = _disk_path(safe)
    return bool(path and path.exists())


def delete(key: str) -> tuple[bool, str]:
    """Remove one object. Deleting something already gone is success.

    With bucket versioning on, this hides the current version rather than
    destroying it — a mis-click stays recoverable for the retention window.
    """
    safe = _safe_key(key)
    if safe is None:
        return False, "unsafe key"

    if is_configured():
        try:
            _r2_required().delete_object(Bucket=settings.R2_BUCKET, Key=safe)
            return True, "r2"
        except _VaultUnavailable:
            return False, "vault unavailable"
        except Exception as exc:
            logger.error("R2 delete failed for %s (%s: %s)", safe, type(exc).__name__, exc)
            return False, f"r2 error: {type(exc).__name__}"

    path = _disk_path(safe)
    if path is None:
        return False, "unsafe key"
    try:
        path.unlink(missing_ok=True)
        return True, "disk"
    except Exception as exc:
        logger.error("disk delete failed for %s (%s: %s)", safe, type(exc).__name__, exc)
        return False, f"disk error: {type(exc).__name__}"


def delete_prefix(prefix: str) -> tuple[int, str]:
    """Remove everything under `prefix`. Returns (count_deleted, detail).

    Used when a patient is deleted, which previously did shutil.rmtree on the
    patient's folder. Object stores have no folders, so "delete the folder"
    means "list the prefix and delete each key".
    """
    safe = _safe_key(prefix)
    if safe is None:
        return 0, "unsafe prefix"
    if not safe.endswith("/"):
        safe += "/"

    if is_configured():
        removed = 0
        try:
            client = _r2_required()
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.R2_BUCKET, Prefix=safe):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if not keys:
                    continue
                # delete_objects caps at 1000 per call; the paginator already
                # yields at most that many.
                client.delete_objects(Bucket=settings.R2_BUCKET,
                                      Delete={"Objects": keys})
                removed += len(keys)
            return removed, "r2"
        except _VaultUnavailable:
            return 0, "vault unavailable"
        except Exception as exc:
            logger.error("R2 delete_prefix failed for %s (%s: %s)",
                         safe, type(exc).__name__, exc)
            return removed, f"r2 error: {type(exc).__name__}"

    path = _disk_path(safe)
    if path is None or not path.exists():
        return 0, "disk"
    try:
        import shutil
        count = sum(1 for p in path.rglob("*") if p.is_file())
        shutil.rmtree(path, ignore_errors=True)
        return count, "disk"
    except Exception as exc:
        logger.error("disk delete_prefix failed for %s (%s: %s)",
                     safe, type(exc).__name__, exc)
        return 0, f"disk error: {type(exc).__name__}"


def list_keys(prefix: str = "") -> list[str]:
    """Every key under `prefix`. For the migration script and tests."""
    safe = _safe_key(prefix) if prefix else ""
    if prefix and safe is None:
        return []

    if is_configured():
        out: list[str] = []
        try:
            paginator = _r2_required().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.R2_BUCKET, Prefix=safe or ""):
                out.extend(o["Key"] for o in page.get("Contents", []))
        except _VaultUnavailable:
            pass
        except Exception as exc:
            logger.error("R2 list failed for %s (%s: %s)", safe, type(exc).__name__, exc)
        return out

    root = _DISK_ROOT / (safe or "")
    if not root.exists():
        return []
    base = _DISK_ROOT.resolve()
    return [str(p.resolve().relative_to(base)) for p in root.rglob("*") if p.is_file()]
