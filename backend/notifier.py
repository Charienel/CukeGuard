"""
notifier.py — sends the "bad cucumber inside the box" SMS alert to the owner.

SIMULATED for now: it just logs to the AlertLog table and prints to console,
so the whole alert flow (detection -> alert -> dashboard log) works end to
end without a real SIM card or SMS account.

TO GO LIVE:
  Pick one:
  - GSM module on the Pi (e.g. SIM800L over UART/serial) -- send AT commands
    directly, or use a library like `gsmmodem`.
  - A cloud SMS API -- for the Philippines, Semaphore
    (https://semaphore.co) or Twilio both work over plain HTTPS, no modem
    needed. Just call their REST endpoint with `requests.post(...)` inside
    `send_sms()` below using your API key.

Either way, only `send_sms()` needs to change -- `notify_bad_condition()`
and everything that calls it stays the same.
"""
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .database import AlertLog


def sms_mode() -> str:
    """Return the configured SMS mode without exposing provider credentials."""
    provider = os.getenv("CUKEGUARD_SMS_PROVIDER", "").lower()
    if provider == "semaphore":
        return "semaphore" if os.getenv("SEMAPHORE_API_KEY") else "semaphore_unconfigured"
    if provider == "iprog":
        return "iprog" if os.getenv("IPROG_API_TOKEN") else "iprog_unconfigured"
    return "simulated"


def _iprog_failure(reason: str, api_token: str, phone_number: str) -> str:
    """Keep provider diagnostics useful without exposing credentials or numbers."""
    safe_reason = reason.replace(api_token, "[redacted]").replace(phone_number, "[redacted]")
    safe_reason = " ".join(safe_reason.split())[:120]
    return f"failed: {safe_reason or 'iProg rejected the request'}"


def _send_iprog_sms(phone_number: str, message: str) -> str:
    """Submit an SMS to iProg and report queue acceptance, not delivery."""
    api_token = os.getenv("IPROG_API_TOKEN")
    if not api_token:
        print("[SMS FAILED] IPROG_API_TOKEN is not configured.")
        return "failed"

    phone_number = phone_number.removeprefix("+")
    payload = {
        "api_token": api_token,
        "phone_number": phone_number,
        "message": message,
    }
    sms_provider = os.getenv("IPROG_SMS_PROVIDER")
    if sms_provider:
        if sms_provider not in {"0", "1", "2"}:
            print("[SMS FAILED] IPROG_SMS_PROVIDER must be 0, 1, or 2.")
            return "failed"
        payload["sms_provider"] = int(sms_provider)

    query = urlencode(payload)
    request = Request(
        f"https://www.iprogsms.com/api/v1/sms_messages?{query}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if isinstance(result, dict) and result.get("status") == 200:
            return "submitted"
        detail = result.get("message") if isinstance(result, dict) else None
        if not isinstance(detail, str):
            detail = "iProg rejected the request"
        return _iprog_failure(detail, api_token, phone_number)
    except HTTPError as error:
        try:
            error_body = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            error_body = {}
        detail = error_body.get("message") if isinstance(error_body, dict) else None
        detail = detail if isinstance(detail, str) else "request rejected"
        return _iprog_failure(f"HTTP {error.code}: {detail}", api_token, phone_number)
    except URLError:
        return _iprog_failure("Could not reach iProg", api_token, phone_number)
    except TimeoutError:
        return _iprog_failure("Timed out contacting iProg", api_token, phone_number)
    except json.JSONDecodeError:
        return _iprog_failure("iProg returned invalid JSON", api_token, phone_number)


def send_sms(phone_number: str, message: str) -> str:
    """Submit through the configured provider, or explicitly mark as simulated."""
    mode = sms_mode()
    if mode == "simulated":
        print(f"[SIMULATED SMS; NOT DELIVERED to {phone_number}] {message}")
        return "simulated"
    if mode == "iprog":
        return _send_iprog_sms(phone_number, message)
    if mode.endswith("_unconfigured"):
        print(f"[SMS FAILED] Configure credentials for {mode.removesuffix('_unconfigured')}.")
        return "failed"

    form_data = {
        "apikey": os.environ["SEMAPHORE_API_KEY"],
        "number": phone_number,
        "message": message,
    }
    sender_name = os.getenv("SEMAPHORE_SENDER_NAME")
    if sender_name:
        form_data["sendername"] = sender_name

    request = Request(
        "https://api.semaphore.co/api/v4/messages",
        data=urlencode(form_data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if isinstance(result, dict) and result.get("error"):
            return "failed"
        if isinstance(result, list) and (
            not result
            or any(item.get("status") == "Failed" for item in result if isinstance(item, dict))
        ):
            return "failed"
        return "submitted"
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        print("[SMS FAILED] Semaphore request did not complete successfully.")
        return "failed"


def notify_bad_condition(
    db, batch, scan_session_id: int, bad_cucumber_numbers: list[int]
) -> AlertLog | None:
    """Called right after a scan finds one or more 'bad' cucumbers.
    Sends (simulated) SMS to the batch's owner_phone and logs it so the
    touchpad can show an alert history."""
    if not bad_cucumber_numbers:
        return None

    phone = batch.owner_phone or "UNSET-NUMBER"
    cucumber_list = ", ".join(f"#{number}" for number in bad_cucumber_numbers)
    message = (
        f"CukeGuard Alert: Batch B-{batch.id:04d} ({batch.batch_code}), "
        f"Scan S-{scan_session_id:04d}: bad cucumber(s) {cucumber_list}, "
        "numbered right-to-left. "
        "Please remove these from storage."
    )
    status = send_sms(phone, message)

    alert = AlertLog(
        batch_id=batch.id,
        scan_session_id=scan_session_id,
        sent_at=datetime.utcnow(),
        phone_number=phone,
        message=message,
        bad_count=len(bad_cucumber_numbers),
        delivery_status=status,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert
