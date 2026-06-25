from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import csv
import io
import os
import json
import logging
import hmac
import hashlib
import sqlite3
import asyncio
import requests
from requests.auth import HTTPBasicAuth
from pathlib import Path
from pydantic import BaseModel, ConfigDict, EmailStr
import uuid
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


# ── SQLite (single-file persistence; survives restarts) ──
DB_PATH = Path(os.environ.get('SQLITE_DB_PATH', str(ROOT_DIR / 'bookings.db')))

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                id                  TEXT PRIMARY KEY,
                service_id          TEXT NOT NULL,
                service_name        TEXT NOT NULL,
                amount_paise        INTEGER NOT NULL,
                currency            TEXT NOT NULL DEFAULT 'INR',
                slot_iso            TEXT NOT NULL,
                customer_name       TEXT NOT NULL,
                customer_email      TEXT NOT NULL,
                customer_phone      TEXT NOT NULL,
                razorpay_order_id   TEXT NOT NULL UNIQUE,
                razorpay_payment_id TEXT,
                status              TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                paid_at             TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_status     ON bookings(status);
            CREATE INDEX IF NOT EXISTS idx_bookings_created_at ON bookings(created_at);
            CREATE INDEX IF NOT EXISTS idx_bookings_order_id   ON bookings(razorpay_order_id);

            -- Free consultation leads from Cal.com (separate from paid puja bookings).
            -- Populated by the Cal.com webhook; cal_uid is the idempotency key.
            CREATE TABLE IF NOT EXISTS consultations (
                id            TEXT PRIMARY KEY,
                cal_uid       TEXT NOT NULL UNIQUE,
                name          TEXT,
                email         TEXT,
                phone         TEXT,
                ceremony      TEXT,
                start_iso     TEXT,
                status        TEXT NOT NULL,   -- booked | rescheduled | cancelled | no_show
                meeting_url   TEXT,             -- video-call join link (Cal metadata.videoCallUrl)
                reminder_sent     INTEGER NOT NULL DEFAULT 0,  -- the ~60-min reminder
                reminder_15_sent  INTEGER NOT NULL DEFAULT 0,  -- the ~15-min reminder
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_consult_status    ON consultations(status);
            CREATE INDEX IF NOT EXISTS idx_consult_start     ON consultations(start_iso);
            CREATE INDEX IF NOT EXISTS idx_consult_cal_uid   ON consultations(cal_uid);

            -- CEO cockpit: simple project task board. Admin-entered (no external
            -- source) — this is the one panel whose data is "real" by being yours.
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                done_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_done ON tasks(done);

            -- Generic cache (used by the SEO panel to store the last Google pull
            -- so cockpit page loads are instant; refreshed by a background task).
            CREATE TABLE IF NOT EXISTS app_cache (
                key        TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
        """)
        # Migrate DBs created before meeting_url / reminder_15_sent existed.
        cols = {r[1] for r in c.execute("PRAGMA table_info(consultations)").fetchall()}
        if 'meeting_url' not in cols:
            c.execute("ALTER TABLE consultations ADD COLUMN meeting_url TEXT")
        if 'reminder_15_sent' not in cols:
            c.execute("ALTER TABLE consultations ADD COLUMN reminder_15_sent INTEGER NOT NULL DEFAULT 0")


def _row_to_api(row: sqlite3.Row) -> dict:
    """Shape DB row back into the nested-customer dict the API used to return."""
    return {
        "id": row["id"],
        "service_id": row["service_id"],
        "service_name": row["service_name"],
        "amount_paise": row["amount_paise"],
        "currency": row["currency"],
        "slot_iso": row["slot_iso"],
        "customer": {
            "name": row["customer_name"],
            "email": row["customer_email"],
            "phone": row["customer_phone"],
        },
        "razorpay_order_id": row["razorpay_order_id"],
        "razorpay_payment_id": row["razorpay_payment_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "paid_at": row["paid_at"],
    }


def db_save_booking(doc: dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO bookings (
                id, service_id, service_name, amount_paise, currency, slot_iso,
                customer_name, customer_email, customer_phone,
                razorpay_order_id, razorpay_payment_id, status, created_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc['id'], doc['service_id'], doc['service_name'],
                doc['amount_paise'], doc['currency'], doc['slot_iso'],
                doc['customer']['name'], doc['customer']['email'], doc['customer']['phone'],
                doc['razorpay_order_id'], doc.get('razorpay_payment_id'),
                doc['status'], doc['created_at'], doc.get('paid_at'),
            ),
        )
    logger.info(
        "[BOOKING CREATED] %s | %s | ₹%.0f | %s | %s <%s>",
        doc['id'], doc['service_name'], doc['amount_paise']/100,
        doc['slot_iso'], doc['customer']['name'], doc['customer']['email'],
    )


def db_update_booking(booking_id: str, order_id: str, updates: dict) -> bool:
    """Update only if (id, order_id) match. Returns True if a row was matched."""
    cols, vals = [], []
    for k, v in updates.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.extend([booking_id, order_id])
    with _conn() as c:
        cur = c.execute(
            f"UPDATE bookings SET {', '.join(cols)} WHERE id = ? AND razorpay_order_id = ?",
            vals,
        )
        matched = cur.rowcount > 0
    if matched:
        logger.info(
            "[BOOKING UPDATED] %s | status=%s | payment_id=%s",
            booking_id, updates.get('status', '?'), updates.get('razorpay_payment_id', '—'),
        )
    return matched


def db_list_bookings(limit: int = 1000) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_api(r) for r in rows]


def db_get_by_order_id(order_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM bookings WHERE razorpay_order_id = ?", (order_id,)
        ).fetchone()
    return _row_to_api(row) if row else None


def db_mark_paid_by_order_id(order_id: str, payment_id: str) -> str:
    """Idempotently mark a booking 'paid' by Razorpay order_id.
    Returns: 'updated' | 'already_paid' | 'not_found'.
    Safe to call multiple times (webhook retries)."""
    paid_at = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            """UPDATE bookings
               SET status = 'paid', razorpay_payment_id = ?, paid_at = ?
               WHERE razorpay_order_id = ? AND status != 'paid'""",
            (payment_id, paid_at, order_id),
        )
        if cur.rowcount > 0:
            return 'updated'
        # rowcount == 0 either means not found OR already paid
        row = c.execute(
            "SELECT status FROM bookings WHERE razorpay_order_id = ?", (order_id,)
        ).fetchone()
    if row is None:
        return 'not_found'
    return 'already_paid'


def db_mark_paid_manual(booking_id: str) -> str:
    """Admin-triggered transition from pending_upi → paid (UPI stopgap flow).
    Refuses to flip any other status. Returns: 'updated' | 'already_paid' | 'not_found' | 'bad_state'."""
    paid_at = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            """UPDATE bookings
               SET status = 'paid', paid_at = ?
               WHERE id = ? AND status = 'pending_upi'""",
            (paid_at, booking_id),
        )
        if cur.rowcount > 0:
            logger.info("[ADMIN MARK PAID] %s", booking_id)
            return 'updated'
        row = c.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        return 'not_found'
    if row['status'] == 'paid':
        return 'already_paid'
    return 'bad_state'


def db_mark_failed_by_order_id(order_id: str, payment_id: str | None, reason: str | None) -> str:
    """Mark booking 'failed'. Refuses to downgrade an already 'paid' booking.
    Returns: 'updated' | 'paid_kept' | 'not_found'."""
    with _conn() as c:
        # Only flip non-paid rows. Anything 'paid' is left alone (auth eventually
        # succeeded; a stray 'failed' event must not erase that).
        cur = c.execute(
            """UPDATE bookings
               SET status = 'failed', razorpay_payment_id = COALESCE(?, razorpay_payment_id)
               WHERE razorpay_order_id = ? AND status != 'paid'""",
            (payment_id, order_id),
        )
        if cur.rowcount > 0:
            return 'updated'
        row = c.execute(
            "SELECT status FROM bookings WHERE razorpay_order_id = ?", (order_id,)
        ).fetchone()
    if row is None:
        return 'not_found'
    if row['status'] == 'paid':
        return 'paid_kept'
    return 'updated'


# ── CONSULTATIONS: persistence ──
IST = ZoneInfo("Asia/Kolkata")


def _consult_row_to_dict(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "cal_uid": row["cal_uid"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "ceremony": row["ceremony"],
        "start_iso": row["start_iso"],
        "status": row["status"],
        "meeting_url": row["meeting_url"] if "meeting_url" in keys else None,
        "reminder_sent": row["reminder_sent"],
        "reminder_15_sent": row["reminder_15_sent"] if "reminder_15_sent" in keys else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def db_create_consultation(doc: dict) -> bool:
    """Insert a consultation. If cal_uid already exists (webhook retry), refresh
    the mutable fields instead and keep reminder_sent. Returns True only when a
    brand-new row was inserted — so the owner alert fires exactly once."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        try:
            c.execute(
                """INSERT INTO consultations
                   (id, cal_uid, name, email, phone, ceremony, start_iso,
                    meeting_url, status, reminder_sent, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (str(uuid.uuid4()), doc['cal_uid'], doc.get('name'), doc.get('email'),
                 doc.get('phone'), doc.get('ceremony'), doc.get('start_iso'),
                 doc.get('meeting_url'), doc.get('status', 'booked'), now, now),
            )
            logger.info("[CONSULT NEW] uid=%s %s <%s> ph=%s link=%s",
                        doc['cal_uid'], doc.get('name'), doc.get('email'),
                        doc.get('phone'), bool(doc.get('meeting_url')))
            return True
        except sqlite3.IntegrityError:
            c.execute(
                """UPDATE consultations
                   SET name=?, email=?, phone=?, ceremony=?, start_iso=?,
                       meeting_url=COALESCE(?, meeting_url), status=?, updated_at=?
                   WHERE cal_uid=?""",
                (doc.get('name'), doc.get('email'), doc.get('phone'), doc.get('ceremony'),
                 doc.get('start_iso'), doc.get('meeting_url'),
                 doc.get('status', 'booked'), now, doc['cal_uid']),
            )
            return False


def db_get_consultation_by_uid(cal_uid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM consultations WHERE cal_uid = ?", (cal_uid,)).fetchone()
    return _consult_row_to_dict(row) if row else None


def db_update_consultation_by_uid(cal_uid: str, updates: dict) -> bool:
    updates = {**updates, 'updated_at': datetime.now(timezone.utc).isoformat()}
    cols = ', '.join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [cal_uid]
    with _conn() as c:
        cur = c.execute(f"UPDATE consultations SET {cols} WHERE cal_uid = ?", vals)
        matched = cur.rowcount > 0
    if matched:
        logger.info("[CONSULT UPDATE] uid=%s %s", cal_uid,
                    {k: v for k, v in updates.items() if k != 'updated_at'})
    return matched


def db_due_reminders() -> list[dict]:
    """Booked consults starting within the (longer) reminder window that still
    need at least one of the two reminders (~60 min and ~15 min before). The loop
    decides which to send. start_iso is stored normalised to UTC (+00:00) so
    string comparison against now is safe."""
    now = datetime.now(timezone.utc)
    lower = now.isoformat()
    upper = (now + timedelta(minutes=REMINDER_LEAD_MINUTES)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM consultations
               WHERE status = 'booked'
                 AND (reminder_sent = 0 OR reminder_15_sent = 0)
                 AND start_iso IS NOT NULL AND start_iso > ? AND start_iso <= ?
               ORDER BY start_iso ASC""",
            (lower, upper),
        ).fetchall()
    return [_consult_row_to_dict(r) for r in rows]


def db_mark_reminder_sent(consult_id: str, kind: str = '60') -> None:
    """kind '60' marks the hour-before reminder; '15' marks the final reminder."""
    col = 'reminder_15_sent' if kind == '15' else 'reminder_sent'
    with _conn() as c:
        c.execute(f"UPDATE consultations SET {col} = 1 WHERE id = ?", (consult_id,))


def db_query_consultations(status: str | None = None, q: str | None = None, limit: int = 1000) -> list[dict]:
    where, vals = [], []
    if status:
        where.append("status = ?"); vals.append(status)
    if q:
        like = f"%{q}%"
        where.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR ceremony LIKE ?)")
        vals.extend([like, like, like, like])
    sql = "SELECT * FROM consultations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_iso DESC LIMIT ?"
    vals.append(limit)
    with _conn() as c:
        rows = c.execute(sql, vals).fetchall()
    return [_consult_row_to_dict(r) for r in rows]


def db_consult_status_counts() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM consultations GROUP BY status").fetchall()
    return {r['status']: r['n'] for r in rows}


# ── CONSULTATIONS: Cal.com payload parsing ──
def _parse_iso_to_utc(s: str | None) -> str | None:
    """Normalise any ISO timestamp (Cal sends '...Z' or with an offset) to a
    consistent UTC '+00:00' string so range comparisons are reliable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    except Exception:
        return s


def parse_cal_booking(payload: dict) -> dict:
    """Pull the fields we care about out of a Cal.com webhook 'payload'. Cal's
    'responses' values can be plain strings or {'value': ...} objects; attendees
    carry name/email/phone too. We probe both shapes."""
    responses = payload.get('responses') or {}
    attendees = payload.get('attendees') or []
    att0 = attendees[0] if attendees else {}

    def rv(key):
        v = responses.get(key)
        return v.get('value') if isinstance(v, dict) else v

    name = rv('name') or att0.get('name')
    email = rv('email') or att0.get('email')

    phone = rv('phone') or rv('attendeePhoneNumber') or rv('smsReminderNumber') or att0.get('phoneNumber')
    if not phone:
        for k, v in responses.items():
            if 'phone' in k.lower():
                phone = v.get('value') if isinstance(v, dict) else v
                if phone:
                    break

    notes = rv('notes') or payload.get('additionalNotes') or ''
    ceremony = notes.strip() if isinstance(notes, str) and notes.strip() else None

    uid = payload.get('uid') or payload.get('bookingId') or payload.get('id')

    # Video-call join link. Cal puts it in metadata.videoCallUrl (Google Meet &
    # Cal Video); fall back to videoCallData.url or a location that's a raw URL.
    metadata = payload.get('metadata') or {}
    location = payload.get('location')
    meeting_url = (
        metadata.get('videoCallUrl')
        or (payload.get('videoCallData') or {}).get('url')
        or (location if isinstance(location, str) and location.startswith('http') else None)
    )

    return {
        'cal_uid': str(uid) if uid is not None else None,
        'name': name,
        'email': email,
        'phone': str(phone) if phone else None,
        'ceremony': ceremony,
        'start_iso': _parse_iso_to_utc(payload.get('startTime')),
        'meeting_url': meeting_url,
    }


def _guest_no_show(payload: dict) -> bool:
    """True when any attendee (the guest) is flagged no-show in the payload."""
    for a in (payload.get('attendees') or []):
        if a.get('noShow') is True:
            return True
    return False


# ── CONSULTATIONS: email (Brevo HTTP API) ──
def _email_ready() -> bool:
    return bool(BREVO_API_KEY and SMTP_FROM)


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'</p>', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one HTML email via Brevo's HTTP API. Never raises — logs and returns
    False on failure so a webhook handler always returns 200 to Cal.com
    regardless of mail outcome. (Railway blocks SMTP, so HTTP API it is.)"""
    if not _email_ready():
        logger.warning("[EMAIL SKIPPED] Brevo not configured; would send '%s' to %s", subject, to)
        return False
    if not to:
        logger.warning("[EMAIL SKIPPED] no recipient for '%s'", subject)
        return False
    payload = {
        "sender": {"name": BRAND_NAME, "email": SMTP_FROM},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text or _strip_html(html),
    }
    try:
        r = requests.post(
            BREVO_API_URL, json=payload,
            headers={"api-key": BREVO_API_KEY, "accept": "application/json",
                     "content-type": "application/json"},
            timeout=20,
        )
        if r.status_code in (200, 201):
            logger.info("[EMAIL SENT] '%s' -> %s (brevo %s)", subject, to, r.status_code)
            return True
        logger.error("[EMAIL FAILED] '%s' -> %s | brevo %s: %s",
                     subject, to, r.status_code, r.text[:300])
        return False
    except Exception:
        logger.exception("[EMAIL FAILED] '%s' -> %s", subject, to)
        return False


def _schedule_email(fn, *args) -> None:
    """Run a blocking email send OFF the request path. SMTP (SSL handshake +
    login + send to the cPanel mail server) can take several seconds; Cal.com
    expects a fast 200 or it retries. So we fire-and-forget on a worker thread
    and let the webhook return immediately."""
    async def _runner():
        try:
            await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("[EMAIL TASK] %s failed", getattr(fn, '__name__', fn))
    try:
        asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        # No running loop (called outside async context) — send inline.
        try:
            fn(*args)
        except Exception:
            logger.exception("[EMAIL] %s failed (sync fallback)", getattr(fn, '__name__', fn))


def _fmt_ist(iso: str | None) -> str:
    if not iso:
        return 'your scheduled time'
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(IST)
        return dt.strftime('%d %b %Y, %I:%M %p IST').lstrip('0')
    except Exception:
        return iso


def _email_shell(heading: str, body_html: str, hero_url: str | None = None) -> str:
    radius = "0" if hero_url else "8px 8px 0 0"
    hero = (f'<img src="{hero_url}" alt="HomePujan sacred fire ritual" width="560" '
            f'style="display:block;width:100%;max-width:560px;height:auto;border:0" />'
            if hero_url else '')
    return f"""<div style="font-family:Georgia,'Times New Roman',serif;max-width:560px;margin:0 auto;color:#2D2D2D">
  <div style="background:#4A0E0E;color:#D4AF37;padding:18px 24px;border-radius:{radius}">
    <h1 style="margin:0;font-size:18px;letter-spacing:.06em">{BRAND_NAME}</h1>
  </div>
  {hero}
  <div style="border:1px solid #E5DED0;border-top:none;border-radius:0 0 8px 8px;padding:24px;line-height:1.6">
    <h2 style="color:#4A0E0E;font-size:16px;margin:0 0 14px">{heading}</h2>
    {body_html}
  </div>
</div>"""


def _owner_detail_table(c: dict) -> str:
    phone = c.get('phone') or '—'
    wa = f'<a href="{_wa_link(phone)}" style="color:#25D366">{phone}</a>' if c.get('phone') else '—'
    def row(label, value):
        return (f'<tr><td style="padding:4px 12px 4px 0;color:#7A6A6A">{label}</td>'
                f'<td style="padding:4px 0"><b>{value}</b></td></tr>')
    join = c.get('meeting_url')
    join_html = f'<a href="{join}" style="color:#4A0E0E">{join}</a>' if join else '—'
    return ('<table style="font-size:14px;margin:6px 0 16px">'
            + row('Name', c.get('name') or '—')
            + row('Phone', wa)
            + row('Email', c.get('email') or '—')
            + row('Note', c.get('ceremony') or '—')
            + row('When', _fmt_ist(c.get('start_iso')))
            + row('Join link', join_html)
            + '</table>')


def _email_owner_new_consult(c: dict) -> None:
    if not OWNER_EMAIL:
        return
    body = (
        "<p>A new free consultation has just been booked.</p>"
        + _owner_detail_table(c)
        + f"<p style='font-size:13px;color:#7A6A6A'>Reminder emails (with the join link) go "
        f"to the guest ~{REMINDER_LEAD_MINUTES} min and ~{REMINDER_FINAL_MINUTES} min before. "
        f"If they don't show, mark them no-show in Cal.com to auto-send the reschedule email.</p>"
    )
    send_email(OWNER_EMAIL, "New consultation booked", _email_shell("New consultation booked", body))


def _email_customer_confirmation(c: dict) -> bool:
    """Branded HomePujan booking confirmation, sent on BOOKING_CREATED — alongside
    Cal.com's own plain confirmation (which free plan can't suppress)."""
    name = c.get('name') or 'ji'
    when = _fmt_ist(c.get('start_iso'))
    join = c.get('meeting_url')
    if join:
        join_block = (
            f"<p><a href='{join}' style='display:inline-block;background:#4A0E0E;"
            f"color:#D4AF37;padding:11px 22px;border-radius:6px;text-decoration:none;"
            f"font-weight:700'>Join the video call</a></p>"
            f"<p style='font-size:13px;color:#7A6A6A'>Save this link — paste it into your "
            f"browser at call time:<br><a href='{join}' style='color:#4A0E0E'>{join}</a></p>"
        )
    else:
        join_block = ("<p>Your video link is in the calendar invite from your booking "
                      "confirmation.</p>")
    ceremony = c.get('ceremony')
    cer_line = f"<p style='color:#7A6A6A;font-size:14px'>{ceremony}</p>" if ceremony else ""
    body = (
        f"<p>Namaste {name},</p>"
        f"<p>Your free 15-minute consultation with a {BRAND_NAME} Gurukul scholar is "
        f"confirmed for <b>{when}</b>.</p>"
        + cer_line
        + join_block
        + "<p><b>What to expect:</b> a calm, unhurried conversation — no sales. Share your "
        "Sankalpa (intention) and the scholar will help you choose the right ceremony, "
        "the auspicious Muhurta, and what it involves.</p>"
        + f"<p>Need a different time? Reschedule here:<br>"
        f"<a href='{CAL_REBOOK_URL}' style='color:#4A0E0E'>{CAL_REBOOK_URL}</a></p>"
        + "<p>We look forward to guiding you.<br>— The HomePujan Gurukul</p>"
    )
    return send_email(c.get('email'), f"Your {BRAND_NAME} consultation is confirmed 🪔",
                      _email_shell("Consultation confirmed", body, hero_url=HERO_IMG_URL))


def _email_customer_reminder(c: dict, soon: bool = False) -> bool:
    """soon=False → the ~1-hour-before reminder; soon=True → the ~15-min-before one."""
    name = c.get('name') or 'ji'
    when = _fmt_ist(c.get('start_iso'))
    join = c.get('meeting_url')
    if join:
        join_block = (
            f"<p><a href='{join}' style='display:inline-block;background:#4A0E0E;"
            f"color:#D4AF37;padding:11px 22px;border-radius:6px;text-decoration:none;"
            f"font-weight:700'>Join the video call</a></p>"
            f"<p style='font-size:13px;color:#7A6A6A'>Or paste this into your browser at "
            f"call time:<br><a href='{join}' style='color:#4A0E0E'>{join}</a></p>"
        )
    else:
        join_block = ("<p>Please join by video using the link in your Cal.com confirmation "
                      "email or calendar invite.</p>")
    lead = (f"<p>Your free 15-minute consultation with a {BRAND_NAME} scholar starts "
            f"<b>in about {REMINDER_FINAL_MINUTES} minutes</b> — at <b>{when}</b>.</p>"
            if soon else
            f"<p>This is a gentle reminder of your free 15-minute consultation with a "
            f"{BRAND_NAME} scholar, coming up at <b>{when}</b>.</p>")
    body = (
        f"<p>Namaste {name},</p>"
        + lead
        + join_block
        + f"<p>If the timing no longer suits you, you may reschedule here:<br>"
        f"<a href='{CAL_REBOOK_URL}' style='color:#4A0E0E'>{CAL_REBOOK_URL}</a></p>"
        f"<p>We look forward to guiding you.</p>"
    )
    subject = (f"Starting soon: your {BRAND_NAME} consultation" if soon
               else f"Reminder: your {BRAND_NAME} consultation is coming up")
    heading = "Starting soon" if soon else "Your consultation is coming up"
    return send_email(c.get('email'), subject, _email_shell(heading, body))


def _email_customer_no_show(c: dict) -> bool:
    name = c.get('name') or 'ji'
    when = _fmt_ist(c.get('start_iso'))
    body = (
        f"<p>Namaste {name},</p>"
        f"<p>We were looking forward to your free consultation, but it seems we "
        f"couldn't connect at {when}. No worries at all — it happens.</p>"
        f"<p>We'd be glad to find a new time that suits you. You can rebook in just a few taps:</p>"
        f"<p><a href='{CAL_REBOOK_URL}' style='display:inline-block;background:#4A0E0E;"
        f"color:#D4AF37;padding:11px 20px;border-radius:6px;text-decoration:none;font-weight:700'>"
        f"Reschedule my consultation</a></p>"
        f"<p>Or simply reply to this email and we'll help you personally.</p>"
    )
    return send_email(c.get('email'), f"We missed you — let's reschedule your consultation",
                      _email_shell("We missed you — let's reschedule", body))


def _email_owner_no_show(c: dict) -> None:
    if not OWNER_EMAIL:
        return
    body = (
        "<p>A guest was marked <b>no-show</b>. A reschedule email has been sent to them automatically.</p>"
        + _owner_detail_table(c)
    )
    send_email(OWNER_EMAIL, "Consultation no-show (reschedule email sent)",
               _email_shell("Guest no-show", body))


# ── App ──
app = FastAPI()
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "HomePujan API"}


@api_router.get("/health")
async def health():
    try:
        with _conn() as c:
            c.execute("SELECT 1").fetchone()
        return {"status": "ok", "db": "sqlite", "db_path": str(DB_PATH)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")


# ── PAYMENT MODE SWITCH ──
# 'razorpay' = full gateway flow. 'upi_qr' = stopgap UPI QR flow (manual mark-paid via /admin).
# Used while waiting for a category-friendly gateway (Cashfree/Instamojo) to approve.
PAYMENT_MODE = os.environ.get('PAYMENT_MODE', 'razorpay').strip().lower()

# ── UPI STOPGAP CONFIG ──
UPI_VPA = os.environ.get('UPI_VPA', '').strip()
UPI_PAYEE_NAME = os.environ.get('UPI_PAYEE_NAME', 'HomePujan').strip()
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '').strip()

# ── RAZORPAY PAYMENT INTEGRATION ──
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '')
RAZORPAY_API_BASE = 'https://api.razorpay.com/v1'

# ── CONSULTATION (Cal.com webhook + email) INTEGRATION ──
# Cal.com signs each webhook with HMAC-SHA256 over the raw body using this shared
# secret (set the same value in Cal.com → Settings → Webhooks).
CAL_WEBHOOK_SECRET = os.environ.get('CAL_WEBHOOK_SECRET', '')
# Public booking page customers are sent back to when they no-show.
CAL_REBOOK_URL = os.environ.get('CAL_REBOOK_URL', 'https://cal.com/homepujan/15min')

# Outbound email. Railway blocks all outbound SMTP ports, so we send via Brevo's
# HTTP API (over HTTPS/443). SMTP_FROM is reused as the verified sender address.
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '').strip()
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
SMTP_FROM = os.environ.get('SMTP_FROM', os.environ.get('SMTP_USER', '')).strip()
OWNER_EMAIL = os.environ.get('OWNER_EMAIL', '').strip()
BRAND_NAME = 'HomePujan'
# Hero banner for branded emails (same sacred-fire image as the site hero).
HERO_IMG_URL = os.environ.get('HERO_IMG_URL',
    'https://images.unsplash.com/photo-1630764883473-e8c2056f0589'
    '?crop=entropy&cs=srgb&fm=jpg&q=80&w=1120&h=360&fit=crop')

# Pre-call reminder: fire when a booked consult starts within this many minutes.
REMINDER_LEAD_MINUTES = int(os.environ.get('REMINDER_LEAD_MINUTES', '60') or '60')   # first nudge
REMINDER_FINAL_MINUTES = int(os.environ.get('REMINDER_FINAL_MINUTES', '15') or '15')  # second nudge
# Poll often enough that the ~15-min nudge lands close to on time.
REMINDER_POLL_SECONDS = int(os.environ.get('REMINDER_POLL_SECONDS', '180') or '180')

# Backend-authoritative price table. Frontend sends service_id only; amount lives here.
SERVICE_CATALOG = {
    'satyanarayan':  {'name': 'Satya Narayan Katha',           'price_paise':  210000},
    'gayatri':       {'name': 'Gayatri Jaap',                  'price_paise':  210000},
    'shaanti':       {'name': 'Shaanti Hawan',                 'price_paise':  210000},
    'vastu':         {'name': 'Vastu Dosh Nivaran',            'price_paise': 1000000},
    'kaalsarp':      {'name': 'Kaal Sarp Puja',                'price_paise':  510000},
    'rudra':         {'name': 'Rudraabhishek',                 'price_paise':  510000},
    'laxmi':         {'name': 'Sri Laxmi Pujan',               'price_paise':  510000},
    'ganesh':        {'name': 'Ganesh Puja',                   'price_paise':  510000},
    'vyapar':        {'name': 'Vyapar Samriddhi Puja',         'price_paise':  210000},
    'karyavikas':    {'name': 'Karya Vikas Pujan',             'price_paise': 1000000},
    'kuberlakshmi':  {'name': 'Dhan Kuber Lakshmi Aradhana',   'price_paise':  510000},
    'sundarkand':    {'name': 'Sundarkand Path',               'price_paise': 1000000},
    'mrityunjay':    {'name': 'Maha Mrityunjay Jaap',          'price_paise':  210000},
    'karnavedh':     {'name': 'Karna Ved Sanskaar',            'price_paise':  510000},
    'agnihotra':     {'name': 'Agnihotra',                     'price_paise':  210000},
    'brahmayajj':    {'name': 'Brahmayajj',                    'price_paise':  210000},
    'gaudaan':       {'name': 'Gau Daan',                      'price_paise':  210000},
    'chhapanbhog':   {'name': 'Chhapan Bhog',                  'price_paise':  210000},
    'janamdiwas':    {'name': 'Janam Diwas Hawan',             'price_paise':  210000},
    'namkaran':      {'name': 'Naam Karan',                    'price_paise':  210000},
    'vivah':         {'name': 'Vivah Sanskaar',                'price_paise': 1100000},
    'maanglik':      {'name': 'Maanglik Dosh Nivaaran',        'price_paise':  510000},
    'putrpraapti':   {'name': 'Putr Praapti Puja',             'price_paise':  510000},
    'lagan':         {'name': 'Lagan',                         'price_paise':  210000},
    'vedaarambh':    {'name': 'Ved Aarambh',                   'price_paise': 1100000},
    'pitrapujan':    {'name': 'Pitra Dosh / Pitra Pujan',      'price_paise':  510000},
    'antiyeshti':    {'name': 'Antiyeshti',                    'price_paise':  210000},
    'grahpravesh':   {'name': 'Grah Pravesh',                  'price_paise':  210000},
    'bhoomipujan':   {'name': 'Bhoomi Pujan',                  'price_paise':  210000},
}


class Customer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    email: EmailStr
    phone: str


class CreateOrderRequest(BaseModel):
    service_id: str
    slot_iso: str
    customer: Customer


class VerifyRequest(BaseModel):
    booking_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class UpiIntentRequest(BaseModel):
    service_id: str
    slot_iso: str
    customer: Customer


@api_router.get("/payments/config")
async def payments_config():
    """Frontend reads this to decide which checkout flow to render.
    Returns mode + the fields that mode needs (Razorpay key, or UPI VPA/payee/whatsapp)."""
    if PAYMENT_MODE == 'upi_qr':
        if not (UPI_VPA and WHATSAPP_NUMBER):
            raise HTTPException(status_code=500, detail="UPI stopgap not fully configured (UPI_VPA / WHATSAPP_NUMBER)")
        return {
            "mode": "upi_qr",
            "upi": {
                "vpa": UPI_VPA,
                "payee_name": UPI_PAYEE_NAME,
                "whatsapp_number": WHATSAPP_NUMBER,
            },
        }
    # default: razorpay
    if not RAZORPAY_KEY_ID:
        raise HTTPException(status_code=500, detail="Razorpay key id not configured")
    return {"mode": "razorpay", "key_id": RAZORPAY_KEY_ID}


@api_router.post("/payments/create-order")
async def create_order(req: CreateOrderRequest):
    service = SERVICE_CATALOG.get(req.service_id)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown service_id")

    amount = service['price_paise']
    if amount < 100:
        raise HTTPException(status_code=400, detail="Amount below Razorpay minimum of 100 paise")

    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise HTTPException(status_code=500, detail="Razorpay credentials missing on server")

    booking_id = str(uuid.uuid4())
    receipt = f"bk_{booking_id[:24]}"

    try:
        rp_response = requests.post(
            f"{RAZORPAY_API_BASE}/orders",
            auth=HTTPBasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json={
                "amount": amount,
                "currency": "INR",
                "receipt": receipt,
                "notes": {
                    "service_id": req.service_id,
                    "booking_id": booking_id,
                    "slot_iso": req.slot_iso,
                },
            },
            timeout=15,
        )
    except requests.RequestException:
        logger.exception("Razorpay order create network failure")
        raise HTTPException(status_code=502, detail="Payment provider unreachable")

    if rp_response.status_code == 401:
        raise HTTPException(status_code=401, detail="Payment provider authentication failed")
    if not rp_response.ok:
        logger.error("Razorpay order create failed: %s %s", rp_response.status_code, rp_response.text)
        raise HTTPException(status_code=500, detail="Failed to create payment order")

    order = rp_response.json()

    booking_doc = {
        "id": booking_id,
        "service_id": req.service_id,
        "service_name": service['name'],
        "amount_paise": amount,
        "currency": "INR",
        "slot_iso": req.slot_iso,
        "customer": req.customer.model_dump(),
        "razorpay_order_id": order["id"],
        "razorpay_payment_id": None,
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paid_at": None,
    }
    db_save_booking(booking_doc)

    return {
        "booking_id": booking_id,
        "order_id": order["id"],
        "amount": amount,
        "currency": "INR",
        "service_name": service['name'],
    }


@api_router.post("/payments/verify")
async def verify_payment(req: VerifyRequest):
    if not (req.razorpay_order_id and req.razorpay_payment_id and req.razorpay_signature and req.booking_id):
        raise HTTPException(status_code=400, detail="Missing payment fields")

    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=500, detail="Razorpay secret missing on server")

    expected_sig = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, req.razorpay_signature):
        db_update_booking(req.booking_id, req.razorpay_order_id, {
            "status": "signature_mismatch",
            "razorpay_payment_id": req.razorpay_payment_id,
        })
        raise HTTPException(status_code=400, detail="Signature verification failed")

    matched = db_update_booking(req.booking_id, req.razorpay_order_id, {
        "status": "paid",
        "razorpay_payment_id": req.razorpay_payment_id,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    })
    if not matched:
        raise HTTPException(status_code=404, detail="Booking not found for this order")

    return {"status": "ok", "booking_id": req.booking_id}


@api_router.post("/payments/upi-intent")
async def create_upi_intent(req: UpiIntentRequest):
    """Stopgap path while a category-friendly gateway is pending KYC.
    Creates a booking row with status 'pending_upi' and returns a UPI deep-link URI
    the frontend renders as a QR. Customer pays via any UPI app, then notifies us
    over WhatsApp; we manually mark the row 'paid' from /admin."""
    if PAYMENT_MODE != 'upi_qr':
        raise HTTPException(status_code=400, detail="UPI stopgap not enabled")
    if not (UPI_VPA and WHATSAPP_NUMBER):
        raise HTTPException(status_code=500, detail="UPI stopgap not configured")

    service = SERVICE_CATALOG.get(req.service_id)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown service_id")

    amount_paise = service['price_paise']
    amount_inr = amount_paise / 100
    booking_id = str(uuid.uuid4())
    reference = f"HP-{booking_id[:6].upper()}"
    # Stuff a unique value into razorpay_order_id to satisfy NOT NULL UNIQUE.
    # Column name is legacy; treat it as "payment order id" generally.
    order_id = f"upi_{booking_id}"

    txn_note = f"{reference} {service['name']}"[:50]
    upi_uri = (
        f"upi://pay?pa={quote(UPI_VPA)}"
        f"&pn={quote(UPI_PAYEE_NAME)}"
        f"&am={amount_inr:.2f}"
        f"&tn={quote(txn_note)}"
        f"&cu=INR"
    )

    booking_doc = {
        "id": booking_id,
        "service_id": req.service_id,
        "service_name": service['name'],
        "amount_paise": amount_paise,
        "currency": "INR",
        "slot_iso": req.slot_iso,
        "customer": req.customer.model_dump(),
        "razorpay_order_id": order_id,
        "razorpay_payment_id": None,
        "status": "pending_upi",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paid_at": None,
    }
    db_save_booking(booking_doc)
    logger.info("[UPI INTENT] %s | ref=%s | ₹%.0f", booking_id, reference, amount_inr)

    return {
        "booking_id": booking_id,
        "reference": reference,
        "upi_uri": upi_uri,
        "vpa": UPI_VPA,
        "payee_name": UPI_PAYEE_NAME,
        "amount_paise": amount_paise,
        "currency": "INR",
        "service_name": service['name'],
        "whatsapp_number": WHATSAPP_NUMBER,
    }


@api_router.get("/payments/bookings")
async def list_bookings():
    bookings = db_list_bookings()
    return {
        "db": "sqlite",
        "count": len(bookings),
        "bookings": bookings,
    }


@api_router.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    """Server-to-server callback from Razorpay. Fires regardless of whether the
    customer's browser comes back to /verify, so it catches payments that would
    otherwise be missed (closed tab, crashed page, dropped network).

    Razorpay POSTs the JSON event body and signs it with the webhook secret
    using HMAC-SHA256, sent in the X-Razorpay-Signature header. We verify the
    signature against the *raw* body (not re-serialised JSON) and refuse the
    request if it doesn't match — that's the only thing keeping random callers
    from flipping bookings to 'paid'.

    Handler is idempotent: Razorpay retries up to a few times on non-2xx, so
    receiving the same event twice must be a no-op. db_mark_paid_by_order_id
    skips rows that are already 'paid', and db_mark_failed_by_order_id refuses
    to downgrade a 'paid' booking back to 'failed'.

    Subscribed events (configure these in Razorpay Dashboard → Settings →
    Webhooks): payment.captured, payment.failed.
    """
    raw_body = await request.body()
    received_sig = request.headers.get('X-Razorpay-Signature', '')

    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("Webhook received but RAZORPAY_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if not received_sig:
        raise HTTPException(status_code=400, detail="Missing X-Razorpay-Signature header")

    expected_sig = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, received_sig):
        logger.warning("Webhook signature mismatch — rejecting")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get('event', '')
    payment_entity = (
        payload.get('payload', {}).get('payment', {}).get('entity', {})
    )
    order_id = payment_entity.get('order_id')
    payment_id = payment_entity.get('id')

    if event == 'payment.captured':
        if not order_id or not payment_id:
            return {"status": "ignored", "reason": "missing order_id or payment_id"}
        result = db_mark_paid_by_order_id(order_id, payment_id)
        logger.info("[WEBHOOK] payment.captured order=%s payment=%s result=%s",
                    order_id, payment_id, result)
        return {"status": "ok", "event": event, "result": result}

    if event == 'payment.failed':
        if not order_id:
            return {"status": "ignored", "reason": "missing order_id"}
        reason = (payment_entity.get('error_description')
                  or payment_entity.get('error_reason'))
        result = db_mark_failed_by_order_id(order_id, payment_id, reason)
        logger.info("[WEBHOOK] payment.failed order=%s reason=%s result=%s",
                    order_id, reason, result)
        return {"status": "ok", "event": event, "result": result}

    # Unrecognised event: still return 200 so Razorpay stops retrying.
    logger.info("[WEBHOOK] ignored event=%s", event)
    return {"status": "ignored", "event": event}


@api_router.post("/cal/webhook")
async def cal_webhook(request: Request):
    """Server-to-server callback from Cal.com for the free consultation event.

    Cal signs the raw body with HMAC-SHA256 using the shared secret, sent in the
    X-Cal-Signature-256 header (set the same secret in Cal.com → Settings →
    Webhooks). We verify against the raw body and reject mismatches — same defence
    as the Razorpay webhook above.

    Handled triggers (subscribe to these in Cal.com):
      BOOKING_CREATED        → store lead + email owner (once)
      BOOKING_RESCHEDULED    → update time, re-arm the reminder
      BOOKING_CANCELLED      → mark cancelled (drops out of reminders)
      BOOKING_NO_SHOW_UPDATED→ when the pundit marks the guest no-show, email the
                               customer a reschedule note + alert the owner
    Idempotent: owner alert only fires on a fresh insert; the no-show email only
    fires on the first transition into 'no_show'.
    """
    raw_body = await request.body()
    received_sig = request.headers.get('X-Cal-Signature-256', '')

    if not CAL_WEBHOOK_SECRET:
        logger.error("Cal webhook received but CAL_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="Cal webhook secret not configured")
    if not received_sig:
        raise HTTPException(status_code=400, detail="Missing X-Cal-Signature-256 header")

    expected_sig = hmac.new(CAL_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, received_sig):
        logger.warning("Cal webhook signature mismatch — rejecting")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    trigger = body.get('triggerEvent', '')
    payload = body.get('payload', {}) or {}
    fields = parse_cal_booking(payload)
    uid = fields.get('cal_uid')

    if not uid:
        logger.info("[CAL WEBHOOK] %s ignored — no booking uid", trigger)
        return {"status": "ignored", "reason": "no uid"}

    if trigger == 'BOOKING_CREATED':
        is_new = db_create_consultation(fields)
        if is_new:
            _schedule_email(_email_owner_new_consult, fields)
            _schedule_email(_email_customer_confirmation, fields)
        return {"status": "ok", "event": trigger, "new": is_new}

    if trigger == 'BOOKING_RESCHEDULED':
        updates = {"start_iso": fields['start_iso'], "status": "booked",
                   "reminder_sent": 0, "reminder_15_sent": 0}
        if fields.get('meeting_url'):  # only overwrite the link if a new one was sent
            updates["meeting_url"] = fields['meeting_url']
        updated = db_update_consultation_by_uid(uid, updates)
        if not updated:
            db_create_consultation(fields)
        return {"status": "ok", "event": trigger}

    if trigger == 'BOOKING_CANCELLED':
        db_update_consultation_by_uid(uid, {"status": "cancelled"})
        return {"status": "ok", "event": trigger}

    if trigger == 'BOOKING_NO_SHOW_UPDATED':
        if not _guest_no_show(payload):
            return {"status": "ok", "event": trigger, "note": "not a guest no-show"}
        existing = db_get_consultation_by_uid(uid)
        if existing and existing['status'] != 'no_show':
            db_update_consultation_by_uid(uid, {"status": "no_show"})
            # Prefer stored contact details; fill any gaps from this payload.
            target = {**existing, **{k: v for k, v in fields.items() if v}}
            _schedule_email(_email_customer_no_show, target)
            _schedule_email(_email_owner_no_show, target)
            logger.info("[CAL WEBHOOK] no-show recovery queued uid=%s", uid)
        return {"status": "ok", "event": trigger}

    logger.info("[CAL WEBHOOK] ignored event=%s", trigger)
    return {"status": "ignored", "event": trigger}


# ── ADMIN DASHBOARD ──
# HTTP Basic auth. Browser shows native login popup. Credentials kept in .env
# so they're never committed; refresh requires server restart.
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
_security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    if not (ADMIN_USERNAME and ADMIN_PASSWORD):
        raise HTTPException(status_code=500, detail="Admin credentials not configured")
    ok_user = hmac.compare_digest(credentials.username.encode(), ADMIN_USERNAME.encode())
    ok_pass = hmac.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="HomePujan Admin"'},
        )
    return credentials.username


def db_query_bookings(status: str | None = None, q: str | None = None, limit: int = 1000) -> list[dict]:
    """Filtered listing for admin. status ∈ {paid, created, failed, signature_mismatch}.
    q does a LIKE match across customer name / email / phone / service_name."""
    where, vals = [], []
    if status:
        where.append("status = ?")
        vals.append(status)
    if q:
        like = f"%{q}%"
        where.append("(customer_name LIKE ? OR customer_email LIKE ? OR customer_phone LIKE ? OR service_name LIKE ?)")
        vals.extend([like, like, like, like])
    sql = "SELECT * FROM bookings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    vals.append(limit)
    with _conn() as c:
        rows = c.execute(sql, vals).fetchall()
    return [_row_to_api(r) for r in rows]


def db_status_counts() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM bookings GROUP BY status").fetchall()
    return {r['status']: r['n'] for r in rows}


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>HomePujan — Admin</title>
<style>
  :root { --maroon: #4A0E0E; --gold: #D4AF37; --cream: #FFFEFB; --ink: #2D2D2D; --line: #E5DED0; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif; margin: 0; background: #F9F4EC; color: var(--ink); }
  header { background: var(--maroon); color: var(--gold); padding: 1.1rem 1.5rem; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-family: 'Cinzel', Georgia, serif; font-size: 1.15rem; margin: 0; letter-spacing: 0.06em; }
  header .meta { font-size: 0.75rem; opacity: 0.85; }
  main { max-width: 1280px; margin: 1.5rem auto; padding: 0 1.5rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem; }
  .stat { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 0.85rem 1rem; }
  .stat .lbl { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.12em; color: #7A6A6A; font-weight: 600; }
  .stat .val { font-family: 'Cinzel', Georgia, serif; font-size: 1.5rem; font-weight: 700; color: var(--maroon); margin-top: 4px; }
  .stat.paid .val { color: #1E7F3E; }
  .stat.failed .val { color: #9B1C1C; }
  .filters { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 0.85rem 1rem; display: flex; gap: 0.65rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }
  .filters form { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; flex: 1; }
  .filters input, .filters select { padding: 0.5rem 0.7rem; border: 1px solid var(--line); border-radius: 6px; font-size: 0.85rem; font-family: inherit; }
  .filters input[type=search] { flex: 1; min-width: 200px; }
  .filters button { padding: 0.5rem 1rem; border: 1px solid var(--maroon); background: var(--maroon); color: var(--gold); font-weight: 600; border-radius: 6px; cursor: pointer; font-size: 0.8rem; letter-spacing: 0.04em; }
  .filters a.export { padding: 0.5rem 1rem; background: transparent; color: var(--maroon); border: 1px solid var(--maroon); border-radius: 6px; text-decoration: none; font-size: 0.8rem; font-weight: 600; }
  .filters a.clear { font-size: 0.78rem; color: #7A6A6A; text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-size: 0.85rem; }
  th, td { padding: 0.65rem 0.85rem; text-align: left; vertical-align: top; border-bottom: 1px solid #F0E9DE; }
  th { background: #F9F4EC; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: #5A4A4A; font-weight: 700; }
  tbody tr:hover { background: #FAF6EE; }
  td.id { font-family: 'SF Mono', Menlo, monospace; font-size: 0.72rem; color: #5A4A4A; }
  td.amt { font-family: 'Cinzel', Georgia, serif; font-weight: 700; color: var(--maroon); white-space: nowrap; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
  .pill.paid { background: #E2F5E8; color: #1E7F3E; }
  .pill.created { background: #FFF4D8; color: #8A6300; }
  .pill.pending_upi { background: #FFEDD5; color: #9A3412; }
  .pill.failed { background: #FDECEC; color: #9B1C1C; }
  .pill.signature_mismatch { background: #FDECEC; color: #9B1C1C; }
  .empty { text-align: center; padding: 3rem; color: #7A6A6A; }
  small.muted { color: #7A6A6A; }
  .wa { color: #25D366; text-decoration: none; }
  .wa:hover { text-decoration: underline; }
  .mark-paid-form { display: inline; margin: 0; }
  .mark-paid-btn { padding: 4px 10px; background: #1E7F3E; color: white; border: none; border-radius: 4px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; cursor: pointer; font-family: inherit; }
  .mark-paid-btn:hover { background: #176430; }
  .mark-paid-btn:disabled { opacity: 0.5; cursor: wait; }
  .flash { background: #E2F5E8; color: #1E7F3E; border: 1px solid #B7E0C4; padding: 0.65rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.85rem; }
  .flash.err { background: #FDECEC; color: #9B1C1C; border-color: #F0C5C5; }
  .stat.pending_upi .val { color: #9A3412; }
</style>
</head>
<body>
<header>
  <h1>HomePujan — Bookings</h1>
  <div class="meta"><a href="/admin/cockpit" style="color:#D4AF37;text-decoration:underline;margin-right:1rem">⌂ Cockpit</a><a href="/admin/consultations" style="color:#D4AF37;text-decoration:underline;margin-right:1rem">Consultations →</a>__COUNT__ rows · DB: __DB__</div>
</header>
<main>
  __FLASH__
  <div class="stats">__STATS__</div>
  <div class="filters">
    <form method="get" action="/admin">
      <input type="search" name="q" placeholder="Search name, email, phone or service…" value="__Q__" autofocus/>
      <select name="status">
        <option value="">All statuses</option>
        <option value="paid"        __SEL_PAID__>Paid</option>
        <option value="pending_upi" __SEL_PENDING_UPI__>Pending UPI</option>
        <option value="created"     __SEL_CREATED__>Created (not yet paid)</option>
        <option value="failed"      __SEL_FAILED__>Failed</option>
        <option value="signature_mismatch" __SEL_SIG__>Signature mismatch</option>
      </select>
      <button type="submit">Apply</button>
      <a class="clear" href="/admin">Clear</a>
    </form>
    <a class="export" href="/admin/export.csv__QS__">Export CSV</a>
  </div>
  __TABLE__
</main>
</body>
</html>"""


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return '—'
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.strftime('%d %b %Y, %H:%M')
    except Exception:
        return iso


def _wa_link(phone: str) -> str:
    digits = ''.join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        digits = '91' + digits
    return f"https://wa.me/{digits}"


def _render_admin(bookings: list[dict], q: str, status: str, flash: str = '', flash_kind: str = 'ok') -> str:
    counts = db_status_counts()
    total = sum(counts.values())
    stat_cards = [
        ('Total', total, ''),
        ('Paid', counts.get('paid', 0), 'paid'),
        ('Pending UPI', counts.get('pending_upi', 0), 'pending_upi'),
        ('Created', counts.get('created', 0), 'created'),
        ('Failed', counts.get('failed', 0) + counts.get('signature_mismatch', 0), 'failed'),
    ]
    stats_html = ''.join(
        f'<div class="stat {cls}"><div class="lbl">{lbl}</div><div class="val">{val}</div></div>'
        for lbl, val, cls in stat_cards
    )

    if not bookings:
        table_html = '<div class="empty">No bookings match your filter.</div>'
    else:
        rows_html = []
        for b in bookings:
            cust = b['customer']
            wa = _wa_link(cust['phone'])
            slot = _fmt_dt(b['slot_iso'])
            created = _fmt_dt(b['created_at'])
            paid = _fmt_dt(b['paid_at']) if b['paid_at'] else '—'
            status_class = b['status']
            # Show "Mark Paid" button for pending_upi rows; ref code in payment-id column.
            if b['status'] == 'pending_upi':
                action_html = (
                    f'<form class="mark-paid-form" method="POST" action="/admin/mark-paid/{b["id"]}">'
                    f'<button class="mark-paid-btn" type="submit" '
                    f'onclick="this.disabled=true;this.textContent=\'…\';this.form.submit();">Mark Paid</button>'
                    f'</form>'
                )
                ref = f"HP-{b['id'][:6].upper()}"
                payment_cell = f'<small class="muted">{ref}</small>'
            else:
                action_html = ''
                payment_cell = f'<span class="id" title="{b["razorpay_payment_id"] or "—"}">{(b["razorpay_payment_id"] or "—")[:14]}</span>'
            rows_html.append(f"""
                <tr>
                  <td class="id" title="{b['id']}">{b['id'][:8]}</td>
                  <td>{b['service_name']}<br/><small class="muted">{b['service_id']}</small></td>
                  <td>{cust['name']}<br/><small class="muted">{cust['email']}</small><br/><a class="wa" href="{wa}" target="_blank" rel="noopener">{cust['phone']} ↗</a></td>
                  <td>{slot}</td>
                  <td class="amt">₹{b['amount_paise']/100:,.0f}</td>
                  <td><span class="pill {status_class}">{b['status'].replace('_', ' ')}</span>{(' ' + action_html) if action_html else ''}</td>
                  <td><small class="muted">{created}</small></td>
                  <td><small class="muted">{paid}</small></td>
                  <td>{payment_cell}</td>
                </tr>
            """)
        table_html = f"""
        <table>
          <thead><tr>
            <th>ID</th><th>Ceremony</th><th>Yajamana</th><th>Slot</th>
            <th>Dakshina</th><th>Status</th><th>Created</th><th>Paid</th><th>Payment ID / Ref</th>
          </tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>"""

    qs_parts = []
    if q: qs_parts.append(f"q={q}")
    if status: qs_parts.append(f"status={status}")
    qs = ('?' + '&'.join(qs_parts)) if qs_parts else ''

    flash_html = ''
    if flash:
        cls = 'flash' + (' err' if flash_kind == 'err' else '')
        flash_html = f'<div class="{cls}">{flash}</div>'

    return (_ADMIN_HTML
        .replace('__COUNT__', str(len(bookings)))
        .replace('__DB__', 'sqlite')
        .replace('__FLASH__', flash_html)
        .replace('__STATS__', stats_html)
        .replace('__TABLE__', table_html)
        .replace('__Q__', q.replace('"', '&quot;') if q else '')
        .replace('__SEL_PAID__',        'selected' if status == 'paid' else '')
        .replace('__SEL_PENDING_UPI__', 'selected' if status == 'pending_upi' else '')
        .replace('__SEL_CREATED__',     'selected' if status == 'created' else '')
        .replace('__SEL_FAILED__',      'selected' if status == 'failed' else '')
        .replace('__SEL_SIG__',         'selected' if status == 'signature_mismatch' else '')
        .replace('__QS__', qs))


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    _user: str = Depends(require_admin),
    q: str = Query('', max_length=120),
    status: str = Query('', max_length=32),
    flash: str = Query('', max_length=120),
    flash_kind: str = Query('ok', max_length=10),
):
    bookings = db_query_bookings(status=status or None, q=q or None)
    return HTMLResponse(_render_admin(bookings, q, status, flash=flash, flash_kind=flash_kind))


@app.post("/admin/mark-paid/{booking_id}")
async def admin_mark_paid(
    booking_id: str,
    request: Request,
    _user: str = Depends(require_admin),
):
    """UPI-stopgap workflow: admin clicks button on a pending_upi row to flip it
    to paid after manually verifying the bank/UPI transaction."""
    from fastapi.responses import RedirectResponse
    result = db_mark_paid_manual(booking_id)
    msg_map = {
        'updated':      ('Marked paid.', 'ok'),
        'already_paid': ('Already paid.', 'ok'),
        'not_found':    ('Booking not found.', 'err'),
        'bad_state':    ('Cannot mark paid — booking is not in pending_upi state.', 'err'),
    }
    msg, kind = msg_map.get(result, ('Unknown result.', 'err'))
    # Preserve filters when bouncing back, but only honour Referer if it points
    # at our own /admin (defends against open-redirect via a forged Referer).
    referer = request.headers.get('referer', '')
    from urllib.parse import urlparse
    parsed = urlparse(referer)
    if parsed.path == '/admin':
        path_and_query = '/admin' + (f'?{parsed.query}' if parsed.query else '')
    else:
        path_and_query = '/admin'
    sep = '&' if '?' in path_and_query else '?'
    redirect_url = f"{path_and_query}{sep}flash={quote(msg)}&flash_kind={kind}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.get("/admin/export.csv", response_class=PlainTextResponse)
async def admin_export_csv(
    _user: str = Depends(require_admin),
    q: str = Query('', max_length=120),
    status: str = Query('', max_length=32),
):
    bookings = db_query_bookings(status=status or None, q=q or None)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'booking_id', 'service_id', 'service_name', 'amount_inr',
        'slot_iso', 'name', 'email', 'phone',
        'status', 'razorpay_order_id', 'razorpay_payment_id',
        'created_at', 'paid_at',
    ])
    for b in bookings:
        c = b['customer']
        w.writerow([
            b['id'], b['service_id'], b['service_name'], f"{b['amount_paise']/100:.2f}",
            b['slot_iso'], c['name'], c['email'], c['phone'],
            b['status'], b['razorpay_order_id'], b['razorpay_payment_id'] or '',
            b['created_at'], b['paid_at'] or '',
        ])
    filename = f"bookings-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    return PlainTextResponse(
        buf.getvalue(),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ── ADMIN: CONSULTATIONS ──
_CONSULT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>HomePujan — Consultations</title>
<style>
  :root { --maroon: #4A0E0E; --gold: #D4AF37; --line: #E5DED0; --ink: #2D2D2D; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif; margin: 0; background: #F9F4EC; color: var(--ink); }
  header { background: var(--maroon); color: var(--gold); padding: 1.1rem 1.5rem; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-family: 'Cinzel', Georgia, serif; font-size: 1.15rem; margin: 0; letter-spacing: 0.06em; }
  header .meta a { color: var(--gold); text-decoration: underline; }
  main { max-width: 1180px; margin: 1.5rem auto; padding: 0 1.5rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem; }
  .stat { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 0.85rem 1rem; }
  .stat .lbl { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.12em; color: #7A6A6A; font-weight: 600; }
  .stat .val { font-family: 'Cinzel', Georgia, serif; font-size: 1.5rem; font-weight: 700; color: var(--maroon); margin-top: 4px; }
  .stat.no_show .val { color: #9B1C1C; }
  .stat.booked .val { color: #1E7F3E; }
  .filters { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 0.85rem 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }
  .filters input, .filters select { padding: 0.5rem 0.7rem; border: 1px solid var(--line); border-radius: 6px; font-size: 0.85rem; font-family: inherit; }
  .filters input[type=search] { flex: 1; min-width: 200px; }
  .filters button { padding: 0.5rem 1rem; border: 1px solid var(--maroon); background: var(--maroon); color: var(--gold); font-weight: 600; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  .filters a.clear { font-size: 0.78rem; color: #7A6A6A; text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-size: 0.85rem; }
  th, td { padding: 0.65rem 0.85rem; text-align: left; vertical-align: top; border-bottom: 1px solid #F0E9DE; }
  th { background: #F9F4EC; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: #5A4A4A; font-weight: 700; }
  tbody tr:hover { background: #FAF6EE; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
  .pill.booked { background: #E2F5E8; color: #1E7F3E; }
  .pill.no_show { background: #FDECEC; color: #9B1C1C; }
  .pill.cancelled { background: #ECECEC; color: #555; }
  .pill.rescheduled { background: #E6EEFB; color: #1D4ED8; }
  .wa { color: #25D366; text-decoration: none; }
  .wa:hover { text-decoration: underline; }
  small.muted { color: #7A6A6A; }
  .empty { text-align: center; padding: 3rem; color: #7A6A6A; }
</style>
</head>
<body>
<header>
  <h1>HomePujan — Consultations</h1>
  <div class="meta"><a href="/admin">← Bookings</a> · __COUNT__ rows</div>
</header>
<main>
  <div class="stats">__STATS__</div>
  <div class="filters">
    <form method="get" action="/admin/consultations" style="display:flex;gap:0.5rem;flex-wrap:wrap;flex:1;align-items:center">
      <input type="search" name="q" placeholder="Search name, email, phone or note…" value="__Q__" autofocus/>
      <select name="status">
        <option value="">All statuses</option>
        <option value="booked"      __SEL_BOOKED__>Booked</option>
        <option value="no_show"     __SEL_NOSHOW__>No-show</option>
        <option value="cancelled"   __SEL_CANCELLED__>Cancelled</option>
        <option value="rescheduled" __SEL_RESCHED__>Rescheduled</option>
      </select>
      <button type="submit">Apply</button>
      <a class="clear" href="/admin/consultations">Clear</a>
    </form>
  </div>
  __TABLE__
</main>
</body>
</html>"""


def _render_consultations(consults: list[dict], q: str, status: str) -> str:
    counts = db_consult_status_counts()
    total = sum(counts.values())
    cards = [
        ('Total', total, ''),
        ('Booked', counts.get('booked', 0), 'booked'),
        ('No-show', counts.get('no_show', 0), 'no_show'),
        ('Cancelled', counts.get('cancelled', 0), 'cancelled'),
        ('Rescheduled', counts.get('rescheduled', 0), 'rescheduled'),
    ]
    stats_html = ''.join(
        f'<div class="stat {cls}"><div class="lbl">{lbl}</div><div class="val">{val}</div></div>'
        for lbl, val, cls in cards
    )

    if not consults:
        table_html = '<div class="empty">No consultations match your filter.</div>'
    else:
        rows = []
        for c in consults:
            phone = c.get('phone') or ''
            contact_phone = (f'<a class="wa" href="{_wa_link(phone)}" target="_blank" rel="noopener">{phone} ↗</a>'
                             if phone else '<small class="muted">no phone</small>')
            r60 = '60✓' if c.get('reminder_sent') else '<span style="color:#bbb">60·</span>'
            r15 = '15✓' if c.get('reminder_15_sent') else '<span style="color:#bbb">15·</span>'
            reminded = f'{r60} {r15}'
            rows.append(f"""
                <tr>
                  <td>{c.get('name') or '—'}<br/><small class="muted">{c.get('email') or '—'}</small><br/>{contact_phone}</td>
                  <td>{c.get('ceremony') or '<small class="muted">—</small>'}</td>
                  <td>{_fmt_ist(c.get('start_iso'))}</td>
                  <td><span class="pill {c.get('status')}">{(c.get('status') or '').replace('_', ' ')}</span></td>
                  <td style="text-align:center">{reminded}</td>
                  <td><small class="muted">{_fmt_dt(c.get('created_at'))}</small></td>
                </tr>
            """)
        table_html = f"""
        <table>
          <thead><tr>
            <th>Guest</th><th>Note</th><th>Slot</th><th>Status</th><th>Reminded</th><th>Booked</th>
          </tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""

    return (_CONSULT_HTML
        .replace('__COUNT__', str(len(consults)))
        .replace('__STATS__', stats_html)
        .replace('__TABLE__', table_html)
        .replace('__Q__', q.replace('"', '&quot;') if q else '')
        .replace('__SEL_BOOKED__',    'selected' if status == 'booked' else '')
        .replace('__SEL_NOSHOW__',    'selected' if status == 'no_show' else '')
        .replace('__SEL_CANCELLED__', 'selected' if status == 'cancelled' else '')
        .replace('__SEL_RESCHED__',   'selected' if status == 'rescheduled' else ''))


@app.get("/admin/consultations", response_class=HTMLResponse)
async def admin_consultations(
    _user: str = Depends(require_admin),
    q: str = Query('', max_length=120),
    status: str = Query('', max_length=32),
):
    consults = db_query_consultations(status=status or None, q=q or None)
    return HTMLResponse(_render_consultations(consults, q, status))


# ── PRE-CALL REMINDER LOOP ──
async def _reminder_loop():
    """Background task: periodically email guests whose consultation starts soon.
    Runs in-process (no extra cron). Skips entirely when SMTP is unconfigured."""
    while True:
        try:
            if _email_ready():
                now = datetime.now(timezone.utc)
                for c in db_due_reminders():
                    try:
                        start = datetime.fromisoformat(c['start_iso'].replace('Z', '+00:00'))
                    except Exception:
                        continue
                    mins_to_start = (start - now).total_seconds() / 60.0
                    if not c.get('email'):
                        # nothing to send to — mark both so it drops out of the query
                        db_mark_reminder_sent(c['id'], '60')
                        db_mark_reminder_sent(c['id'], '15')
                        continue
                    if mins_to_start <= REMINDER_FINAL_MINUTES:
                        # Final (~15-min) nudge. Also marks the hour-one done so the
                        # row drops out (and last-minute bookings get just one email).
                        if not c.get('reminder_15_sent'):
                            if await asyncio.to_thread(_email_customer_reminder, c, True):
                                db_mark_reminder_sent(c['id'], '15')
                                db_mark_reminder_sent(c['id'], '60')
                    elif not c.get('reminder_sent'):
                        # First (~hour-before) nudge.
                        if await asyncio.to_thread(_email_customer_reminder, c, False):
                            db_mark_reminder_sent(c['id'], '60')
        except Exception:
            logger.exception("[REMINDER LOOP] iteration failed")
        await asyncio.sleep(REMINDER_POLL_SECONDS)


# ── SEO & TRAFFIC DATA (Google Search Console + GA4) ─────────────────────────
# Ported from tools/google_pull.py to run server-side. READ-ONLY. Cached in
# SQLite (app_cache) so the cockpit is instant; refreshed by a background task.
# Degrades gracefully: missing creds or API errors are captured, never crash.

GSC_SITE_URL = os.environ.get('GSC_SITE_URL', '')
GA4_PROPERTY_ID = os.environ.get('GA4_PROPERTY_ID', '')
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get('GOOGLE_OAUTH_REFRESH_TOKEN', '')
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]


def _seo_creds_ready() -> bool:
    return all([GSC_SITE_URL, GA4_PROPERTY_ID, GOOGLE_OAUTH_CLIENT_ID,
                GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN])


def _google_creds():
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_GOOGLE_SCOPES,
    )


def fetch_seo_snapshot(days: int = 28) -> dict:
    """Pull GSC + GA4 into one JSON-able dict. Per-source errors are captured."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    s, e = start.isoformat(), end.isoformat()
    snap = {"days": days, "start": s, "end": e,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "gsc": None, "ga4": None, "gsc_error": None, "ga4_error": None}
    if not _seo_creds_ready():
        snap["gsc_error"] = snap["ga4_error"] = "Google credentials not configured on this server"
        return snap
    creds = _google_creds()

    try:
        from googleapiclient.discovery import build
        svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

        def gq(dims, limit=10):
            return svc.searchanalytics().query(siteUrl=GSC_SITE_URL, body={
                "startDate": s, "endDate": e, "dimensions": dims, "rowLimit": limit,
            }).execute().get("rows", [])

        tot = svc.searchanalytics().query(siteUrl=GSC_SITE_URL, body={
            "startDate": s, "endDate": e}).execute().get("rows", [{}])
        t = tot[0] if tot else {}
        snap["gsc"] = {
            "clicks": int(t.get("clicks", 0)),
            "impressions": int(t.get("impressions", 0)),
            "ctr": float(t.get("ctr", 0.0)),
            "position": float(t.get("position", 0.0)),
            "queries": [{"q": r["keys"][0], "clicks": int(r["clicks"]),
                         "impressions": int(r["impressions"]), "position": float(r["position"])}
                        for r in gq(["query"])],
            "pages": [{"url": r["keys"][0], "clicks": int(r["clicks"]),
                       "impressions": int(r["impressions"]), "position": float(r["position"])}
                      for r in gq(["page"])],
        }
    except Exception as ex:
        snap["gsc_error"] = f"{type(ex).__name__}: {ex}"[:300]

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric)
        client = BetaAnalyticsDataClient(credentials=creds)

        def run(dims, mets, limit=15):
            return client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                date_ranges=[DateRange(start_date=s, end_date=e)],
                dimensions=[Dimension(name=d) for d in dims],
                metrics=[Metric(name=m) for m in mets], limit=limit))

        ch = run(["sessionDefaultChannelGroup"], ["sessions", "totalUsers", "conversions"])
        channels = [{"name": r.dimension_values[0].value,
                     "sessions": int(float(r.metric_values[0].value or 0)),
                     "users": int(float(r.metric_values[1].value or 0)),
                     "conversions": int(float(r.metric_values[2].value or 0))}
                    for r in ch.rows]
        ev = run(["eventName"], ["eventCount", "conversions"], limit=25)
        events = [{"name": r.dimension_values[0].value,
                   "count": int(float(r.metric_values[0].value or 0)),
                   "conversions": int(float(r.metric_values[1].value or 0))}
                  for r in ev.rows]
        snap["ga4"] = {
            "sessions": sum(c["sessions"] for c in channels),
            "users": sum(c["users"] for c in channels),
            "conversions": sum(c["conversions"] for c in channels),
            "channels": channels,
            "events": [e2 for e2 in events if e2["conversions"] > 0],
        }
    except Exception as ex:
        snap["ga4_error"] = f"{type(ex).__name__}: {ex}"[:300]

    return snap


def _cache_get(key: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT data, fetched_at FROM app_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except Exception:
        return None


def _cache_set(key: str, data: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO app_cache(key, data, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
            (key, json.dumps(data), datetime.now(timezone.utc).isoformat()))


def refresh_seo_cache(days: int = 28) -> dict:
    snap = fetch_seo_snapshot(days)
    _cache_set("seo_snapshot", snap)
    return snap


def get_seo_cached() -> dict | None:
    return _cache_get("seo_snapshot")


async def _seo_refresh_loop():
    """Refresh the SEO snapshot at startup, then every 6h. Runs the blocking
    Google calls in a thread so the event loop is never stalled."""
    while True:
        try:
            if _seo_creds_ready():
                await asyncio.to_thread(refresh_seo_cache, 28)
                logger.info("SEO snapshot refreshed")
            else:
                logger.info("SEO refresh skipped — Google creds not configured")
        except Exception as e:
            logger.warning("SEO refresh failed: %s", e)
        await asyncio.sleep(6 * 3600)


def _render_seo_section(seo: dict | None) -> str:
    if not seo:
        if not _seo_creds_ready():
            return ('<div class="seclabel">SEO &amp; traffic</div>'
                    '<div class="card greyed">Google credentials are not on this server yet — '
                    'add the Google OAuth env vars on Railway to switch this on.</div>')
        return ('<div class="seclabel">SEO &amp; traffic</div>'
                '<div class="card greyed">Not fetched yet — hit Refresh in a moment.</div>')

    fetched = _fmt_dt(seo.get("fetched_at")) if seo.get("fetched_at") else '—'
    days = seo.get("days", 28)
    refresh_btn = ('<form method="post" action="/admin/seo/refresh" style="margin:0">'
                   '<button type="submit" style="background:transparent;border:1px solid #C9BFA8;'
                   'color:#7A6A6A;border-radius:6px;padding:4px 11px;font-size:.74rem;cursor:pointer">'
                   '↻ Refresh</button></form>')
    head = (f'<div class="subhead"><div class="seclabel" style="margin:0">SEO &amp; traffic '
            f'<span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">'
            f'· last {days} days · updated {fetched}</span></div>{refresh_btn}</div>')

    g = seo.get("gsc")
    if g:
        gsc_cards = (
            '<div class="mcs">'
            f'<div class="mc"><div class="k">Clicks (Google search)</div><div class="v maroon">{g["clicks"]:,}</div></div>'
            f'<div class="mc"><div class="k">Impressions</div><div class="v maroon">{g["impressions"]:,}</div></div>'
            f'<div class="mc"><div class="k">Click-through rate</div><div class="v maroon">{g["ctr"]:.1%}</div></div>'
            f'<div class="mc"><div class="k">Avg position</div><div class="v maroon">{g["position"]:.1f}</div></div>'
            '</div>')
        rows_q = ''.join(
            f'<div class="row"><span>{_esc(x["q"])}</span>'
            f'<span class="muted">{x["clicks"]} clk · {x["impressions"]} imp · pos {x["position"]:.1f}</span></div>'
            for x in g["queries"][:8]) or '<div class="muted">No search queries yet.</div>'
        gsc_block = (gsc_cards
                     + '<div class="card" style="margin-top:.7rem"><div class="k" style="margin-bottom:.3rem">Top search terms bringing people to you</div>'
                     + rows_q + '</div>')
    else:
        gsc_block = f'<div class="card greyed">Search Console error — {_esc(seo.get("gsc_error") or "unknown")}</div>'

    a = seo.get("ga4")
    if a:
        ga_cards = (
            '<div class="mcs">'
            f'<div class="mc"><div class="k">Visitors</div><div class="v green">{a["users"]:,}</div></div>'
            f'<div class="mc"><div class="k">Sessions</div><div class="v green">{a["sessions"]:,}</div></div>'
            f'<div class="mc"><div class="k">Conversions</div><div class="v green">{a["conversions"]:,}</div></div>'
            '</div>')
        rows_ch = ''.join(
            f'<div class="row"><span>{_esc(c["name"])}</span>'
            f'<span class="muted">{c["sessions"]} sessions · {c["users"]} visitors · {c["conversions"]} conv</span></div>'
            for c in a["channels"][:8]) or '<div class="muted">No channel data.</div>'
        if a["events"]:
            rows_ev = ''.join(
                f'<div class="row"><span>{_esc(ev["name"])}</span>'
                f'<span class="ok">{ev["conversions"]} conversions</span></div>'
                for ev in a["events"][:8])
            ev_block = ('<div class="card" style="margin-top:.7rem"><div class="k" style="margin-bottom:.3rem">Key conversions</div>'
                        + rows_ev + '</div>')
        else:
            ev_block = ''
        ga_block = (ga_cards
                    + '<div class="card" style="margin-top:.7rem"><div class="k" style="margin-bottom:.3rem">Where visitors come from</div>'
                    + rows_ch + '</div>' + ev_block)
    else:
        ga_block = f'<div class="card greyed">Analytics error — {_esc(seo.get("ga4_error") or "unknown")}</div>'

    return head + gsc_block + '<div style="height:.5rem"></div>' + ga_block


# ── CEO COCKPIT ──────────────────────────────────────────────────────────────
# One authenticated hub answering "is everything actually working?" alongside
# live money / consultation numbers and a simple project task board. Every figure
# is read from a real source (DB, env config, a live HTTP check) — nothing mocked.

def _esc(s: str) -> str:
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def db_list_tasks() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY done ASC, created_at DESC").fetchall()
    return [dict(r) for r in rows]


def db_add_task(title: str) -> None:
    title = (title or '').strip()[:300]
    if not title:
        return
    with _conn() as c:
        c.execute("INSERT INTO tasks (title, done, created_at) VALUES (?, 0, ?)",
                  (title, datetime.now(timezone.utc).isoformat()))


def db_toggle_task(task_id: int) -> None:
    with _conn() as c:
        row = c.execute("SELECT done FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return
        new_done = 0 if row['done'] else 1
        done_at = datetime.now(timezone.utc).isoformat() if new_done else None
        c.execute("UPDATE tasks SET done=?, done_at=? WHERE id=?", (new_done, done_at, task_id))


def db_delete_task(task_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def _check_site(url: str) -> tuple[bool, str]:
    """Best-effort live check of the public site. Never raises into the page."""
    try:
        r = requests.get(url, timeout=4, allow_redirects=True)
        return (r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        return (False, type(e).__name__)


_COCKPIT_STYLE = """<style>
  :root { --maroon:#4A0E0E; --gold:#D4AF37; --ink:#2D2D2D; --line:#E5DED0; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif; margin:0; background:#F9F4EC; color:var(--ink); }
  header { background:var(--maroon); color:var(--gold); padding:1.1rem 1.5rem; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  header h1 { font-family:'Cinzel',Georgia,serif; font-size:1.15rem; margin:0; letter-spacing:.06em; }
  header nav a { color:var(--gold); text-decoration:none; font-size:.8rem; margin-left:1.1rem; border-bottom:1px solid transparent; }
  header nav a:hover { border-bottom-color:var(--gold); }
  main { max-width:1180px; margin:1.5rem auto; padding:0 1.5rem; }
  .seclabel { font-size:.7rem; text-transform:uppercase; letter-spacing:.12em; color:#7A6A6A; font-weight:700; margin:1.6rem 0 .6rem; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:1rem 1.15rem; }
  .row { display:flex; align-items:center; justify-content:space-between; padding:.5rem 0; border-bottom:1px solid #F0E9DE; font-size:.9rem; gap:1rem; }
  .row:last-child { border-bottom:none; }
  .ok { color:#1E7F3E; font-weight:700; }
  .warn { color:#9A6700; font-weight:700; }
  .bad { color:#9B1C1C; font-weight:700; }
  .muted { color:#7A6A6A; }
  .chips { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.7rem; }
  .chip { font-size:.78rem; padding:4px 10px; border-radius:999px; font-weight:600; }
  .chip.on { background:#E2F5E8; color:#1E7F3E; }
  .chip.off { background:#F1EEE8; color:#8A8175; }
  .mcs { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.7rem; }
  .mc { background:#fff; border:1px solid var(--line); border-radius:10px; padding:.85rem 1rem; }
  .mc .k { font-size:.66rem; text-transform:uppercase; letter-spacing:.1em; color:#7A6A6A; font-weight:700; }
  .mc .v { font-family:'Cinzel',Georgia,serif; font-size:1.5rem; font-weight:700; margin-top:3px; }
  .v.green { color:#1E7F3E; } .v.amber { color:#9A6700; } .v.maroon { color:var(--maroon); } .v.grey { color:#8A8175; }
  ul.tasks { list-style:none; margin:0; padding:0; }
  ul.tasks li { display:flex; align-items:center; gap:.6rem; padding:.5rem 0; border-bottom:1px solid #F0E9DE; font-size:.92rem; }
  ul.tasks li.done span.t { text-decoration:line-through; color:#9A9186; }
  ul.tasks form { display:inline; margin:0; }
  .tbtn { border:none; background:transparent; cursor:pointer; font-size:1rem; padding:2px 6px; line-height:1; }
  .tbtn.chk { color:#1E7F3E; } .tbtn.del { color:#B07A5A; margin-left:auto; }
  .addform { display:flex; gap:.5rem; margin-bottom:.9rem; }
  .addform input { flex:1; padding:.55rem .7rem; border:1px solid var(--line); border-radius:7px; font-size:.9rem; font-family:inherit; }
  .addform button { padding:.55rem 1.2rem; background:var(--maroon); color:var(--gold); border:none; border-radius:7px; font-weight:600; cursor:pointer; }
  .subhead { display:flex; align-items:center; justify-content:space-between; gap:1rem; margin:1.6rem 0 .6rem; }
  .greyed { opacity:.62; font-size:.86rem; line-height:1.8; }
  .src { font-size:.7rem; color:#9A9186; margin-top:.6rem; }
</style>"""


def _render_cockpit() -> str:
    with _conn() as c:
        paid = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_paise),0) s FROM bookings WHERE status='paid'").fetchone()
        pend = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_paise),0) s FROM bookings WHERE status='pending_upi'").fetchone()
        crea = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_paise),0) s FROM bookings WHERE status='created'").fetchone()
    cc = db_consult_status_counts()
    consult_total = sum(cc.values())
    booked = cc.get('booked', 0)

    def has(k):
        return bool(os.environ.get(k, '').strip())

    site_ok, site_detail = _check_site('https://homepujan.com')
    pay_mode = os.environ.get('PAYMENT_MODE', 'unset')
    commit = (os.environ.get('RAILWAY_GIT_COMMIT_SHA', '') or '')[:7] or 'local'

    site_cls = 'ok' if site_ok else 'bad'
    site_word = 'up' if site_ok else 'DOWN'
    pay_stopgap = pay_mode == 'upi_qr'
    pay_cls = 'warn' if pay_stopgap else 'ok'
    pay_word = 'UPI stopgap (no card gateway yet)' if pay_stopgap else f'card gateway · {pay_mode}'

    integrations = [
        ('Razorpay', has('RAZORPAY_KEY_ID') and has('RAZORPAY_KEY_SECRET')),
        ('UPI payment', has('UPI_VPA')),
        ('WhatsApp', has('WHATSAPP_NUMBER')),
        ('Admin login', has('ADMIN_PASSWORD')),
        ('Cal.com webhook', has('CAL_WEBHOOK_SECRET')),
        ('Email alerts', _email_ready()),
        ('Google GSC/GA4', has('GOOGLE_OAUTH_REFRESH_TOKEN')),
        ('Google Ads', has('GOOGLE_ADS_DEVELOPER_TOKEN')),
    ]
    chips = ''.join(
        f'<span class="chip {"on" if on else "off"}">{"● " if on else "○ "}{name}</span>'
        for name, on in integrations
    )

    seo_html = _render_seo_section(get_seo_cached())

    tasks = db_list_tasks()
    open_count = sum(1 for t in tasks if not t['done'])
    if tasks:
        task_items = ''.join(
            f'<li class="{"done" if t["done"] else ""}">'
            f'<form method="post" action="/admin/tasks/{t["id"]}/toggle"><button class="tbtn chk" type="submit" title="toggle done">{"✓" if t["done"] else "○"}</button></form>'
            f'<span class="t">{_esc(t["title"])}</span>'
            f'<form method="post" action="/admin/tasks/{t["id"]}/delete"><button class="tbtn del" type="submit" title="delete">✕</button></form>'
            f'</li>'
            for t in tasks
        )
    else:
        task_items = '<li class="muted" style="border:none">No tasks yet — add your first one above.</li>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>HomePujan — CEO Cockpit</title>
{_COCKPIT_STYLE}
</head><body>
<header>
  <h1>HomePujan — CEO Cockpit</h1>
  <nav><a href="/admin">Bookings →</a><a href="/admin/consultations">Consultations →</a></nav>
</header>
<main>
  <div class="seclabel">Is it actually working?</div>
  <div class="card">
    <div class="row"><span>Live site (homepujan.com)</span><span class="{site_cls}">{site_word} · {site_detail}</span></div>
    <div class="row"><span>Backend (this server)</span><span class="ok">up · serving this page</span></div>
    <div class="row"><span>Payment mode</span><span class="{pay_cls}">{pay_word}</span></div>
    <div class="row"><span>Deployed version</span><span class="muted">{commit}</span></div>
    <div class="chips">{chips}</div>
    <div class="src">● connected · ○ not connected — read live from server config &amp; a real HTTP check, just now</div>
  </div>

  <div class="seclabel">Money (live bookings)</div>
  <div class="mcs">
    <div class="mc"><div class="k">Collected (paid)</div><div class="v green">₹{paid['s']/100:,.0f}</div><div class="muted">{paid['n']} orders</div></div>
    <div class="mc"><div class="k">Pending UPI — confirm</div><div class="v amber">₹{pend['s']/100:,.0f}</div><div class="muted">{pend['n']} orders</div></div>
    <div class="mc"><div class="k">Abandoned</div><div class="v grey">₹{crea['s']/100:,.0f}</div><div class="muted">{crea['n']} orders</div></div>
    <div class="mc"><div class="k">Consultations</div><div class="v maroon">{consult_total}</div><div class="muted">{booked} booked</div></div>
  </div>
  <div class="src">source: live bookings.db &amp; consultations table</div>

  {seo_html}

  <div class="seclabel">Project tasks</div>
  <div class="card">
    <form class="addform" method="post" action="/admin/tasks/add">
      <input name="title" placeholder="Add a task… e.g. confirm the pending UPI payments" maxlength="300" required/>
      <button type="submit">Add</button>
    </form>
    <ul class="tasks">{task_items}</ul>
    <div class="src">{open_count} open · your own board, stored on the server</div>
  </div>

  <div class="seclabel">Not connected yet — shown honestly, never faked</div>
  <div class="card greyed">
    Social media — connect later<br/>
    Google Ads — pending Google Basic-access approval
  </div>
</main>
</body></html>"""


@app.get("/admin/cockpit", response_class=HTMLResponse)
async def admin_cockpit(_user: str = Depends(require_admin)):
    return HTMLResponse(_render_cockpit())


@app.post("/admin/seo/refresh")
async def admin_seo_refresh(_user: str = Depends(require_admin)):
    from fastapi.responses import RedirectResponse
    try:
        await asyncio.to_thread(refresh_seo_cache, 28)
    except Exception as e:
        logger.warning("Manual SEO refresh failed: %s", e)
    return RedirectResponse(url="/admin/cockpit", status_code=303)


@app.post("/admin/tasks/add")
async def admin_task_add(request: Request, _user: str = Depends(require_admin)):
    from fastapi.responses import RedirectResponse
    from urllib.parse import parse_qs
    raw = (await request.body()).decode('utf-8', 'ignore')
    title = parse_qs(raw).get('title', [''])[0]
    db_add_task(title)
    return RedirectResponse(url="/admin/cockpit", status_code=303)


@app.post("/admin/tasks/{task_id}/toggle")
async def admin_task_toggle(task_id: int, _user: str = Depends(require_admin)):
    from fastapi.responses import RedirectResponse
    db_toggle_task(task_id)
    return RedirectResponse(url="/admin/cockpit", status_code=303)


@app.post("/admin/tasks/{task_id}/delete")
async def admin_task_delete(task_id: int, _user: str = Depends(require_admin)):
    from fastapi.responses import RedirectResponse
    db_delete_task(task_id)
    return RedirectResponse(url="/admin/cockpit", status_code=303)


# Mount router + CORS + startup
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info("SQLite ready at %s", DB_PATH)
    asyncio.create_task(_reminder_loop())
    logger.info("Consultation reminder loop started (lead=%dm, poll=%ds, email=%s)",
                REMINDER_LEAD_MINUTES, REMINDER_POLL_SECONDS, "on" if _email_ready() else "off")
    asyncio.create_task(_seo_refresh_loop())
    logger.info("SEO refresh loop started (creds=%s)", "on" if _seo_creds_ready() else "off")
