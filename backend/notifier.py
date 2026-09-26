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
from datetime import datetime

from .database import AlertLog


def send_sms(phone_number: str, message: str) -> str:
    """SIMULATED SMS send. Replace the body with a real GSM/API call.
    Returns 'sent' or 'failed'."""
    print(f"[SIMULATED SMS to {phone_number}] {message}")
    return "sent"


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
