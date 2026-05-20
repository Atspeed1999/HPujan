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
import requests
from requests.auth import HTTPBasicAuth
from pathlib import Path
from pydantic import BaseModel, ConfigDict, EmailStr
import uuid
from datetime import datetime, timezone


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
        """)


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


# ── RAZORPAY PAYMENT INTEGRATION ──
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '')
RAZORPAY_API_BASE = 'https://api.razorpay.com/v1'

# Backend-authoritative price table. Frontend sends service_id only; amount lives here.
SERVICE_CATALOG = {
    'satyanarayan':  {'name': 'Satya Narayan Katha',           'price_paise':  110000},
    'gayatri':       {'name': 'Gayatri Jaap',                  'price_paise':  110000},
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
    'mrityunjay':    {'name': 'Maha Mrityunjay Jaap',          'price_paise':  110000},
    'karnavedh':     {'name': 'Karna Ved Sanskaar',            'price_paise':  510000},
    'agnihotra':     {'name': 'Agnihotra',                     'price_paise':  110000},
    'brahmayajj':    {'name': 'Brahmayajj',                    'price_paise':  210000},
    'gaudaan':       {'name': 'Gau Daan',                      'price_paise':  210000},
    'chhapanbhog':   {'name': 'Chhapan Bhog',                  'price_paise':  110000},
    'janamdiwas':    {'name': 'Janam Diwas Hawan',             'price_paise':  210000},
    'namkaran':      {'name': 'Naam Karan',                    'price_paise':  210000},
    'vivah':         {'name': 'Vivah Sanskaar',                'price_paise': 1100000},
    'maanglik':      {'name': 'Maanglik Dosh Nivaaran',        'price_paise':  510000},
    'putrpraapti':   {'name': 'Putr Praapti Puja',             'price_paise':  510000},
    'lagan':         {'name': 'Lagan',                         'price_paise':  210000},
    'vedaarambh':    {'name': 'Ved Aarambh',                   'price_paise': 1100000},
    'pitrapujan':    {'name': 'Pitra Dosh / Pitra Pujan',      'price_paise':  510000},
    'antiyeshti':    {'name': 'Antiyeshti',                    'price_paise':  110000},
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


@api_router.get("/payments/config")
async def payments_config():
    if not RAZORPAY_KEY_ID:
        raise HTTPException(status_code=500, detail="Razorpay key id not configured")
    return {"key_id": RAZORPAY_KEY_ID}


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
  .pill.failed { background: #FDECEC; color: #9B1C1C; }
  .pill.signature_mismatch { background: #FDECEC; color: #9B1C1C; }
  .empty { text-align: center; padding: 3rem; color: #7A6A6A; }
  small.muted { color: #7A6A6A; }
  .wa { color: #25D366; text-decoration: none; }
  .wa:hover { text-decoration: underline; }
</style>
</head>
<body>
<header>
  <h1>HomePujan — Bookings</h1>
  <div class="meta">__COUNT__ rows · DB: __DB__</div>
</header>
<main>
  <div class="stats">__STATS__</div>
  <div class="filters">
    <form method="get" action="/admin">
      <input type="search" name="q" placeholder="Search name, email, phone or service…" value="__Q__" autofocus/>
      <select name="status">
        <option value="">All statuses</option>
        <option value="paid"     __SEL_PAID__>Paid</option>
        <option value="created"  __SEL_CREATED__>Created (not yet paid)</option>
        <option value="failed"   __SEL_FAILED__>Failed</option>
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


def _render_admin(bookings: list[dict], q: str, status: str) -> str:
    counts = db_status_counts()
    total = sum(counts.values())
    stat_cards = [
        ('Total', total, ''),
        ('Paid', counts.get('paid', 0), 'paid'),
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
            rows_html.append(f"""
                <tr>
                  <td class="id" title="{b['id']}">{b['id'][:8]}</td>
                  <td>{b['service_name']}<br/><small class="muted">{b['service_id']}</small></td>
                  <td>{cust['name']}<br/><small class="muted">{cust['email']}</small><br/><a class="wa" href="{wa}" target="_blank" rel="noopener">{cust['phone']} ↗</a></td>
                  <td>{slot}</td>
                  <td class="amt">₹{b['amount_paise']/100:,.0f}</td>
                  <td><span class="pill {status_class}">{b['status'].replace('_', ' ')}</span></td>
                  <td><small class="muted">{created}</small></td>
                  <td><small class="muted">{paid}</small></td>
                  <td class="id" title="{b['razorpay_payment_id'] or '—'}">{(b['razorpay_payment_id'] or '—')[:14]}</td>
                </tr>
            """)
        table_html = f"""
        <table>
          <thead><tr>
            <th>ID</th><th>Ceremony</th><th>Yajamana</th><th>Slot</th>
            <th>Dakshina</th><th>Status</th><th>Created</th><th>Paid</th><th>Payment ID</th>
          </tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>"""

    qs_parts = []
    if q: qs_parts.append(f"q={q}")
    if status: qs_parts.append(f"status={status}")
    qs = ('?' + '&'.join(qs_parts)) if qs_parts else ''

    return (_ADMIN_HTML
        .replace('__COUNT__', str(len(bookings)))
        .replace('__DB__', 'sqlite')
        .replace('__STATS__', stats_html)
        .replace('__TABLE__', table_html)
        .replace('__Q__', q.replace('"', '&quot;') if q else '')
        .replace('__SEL_PAID__',    'selected' if status == 'paid' else '')
        .replace('__SEL_CREATED__', 'selected' if status == 'created' else '')
        .replace('__SEL_FAILED__',  'selected' if status == 'failed' else '')
        .replace('__SEL_SIG__',     'selected' if status == 'signature_mismatch' else '')
        .replace('__QS__', qs))


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    _user: str = Depends(require_admin),
    q: str = Query('', max_length=120),
    status: str = Query('', max_length=32),
):
    bookings = db_query_bookings(status=status or None, q=q or None)
    return HTMLResponse(_render_admin(bookings, q, status))


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
