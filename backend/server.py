from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
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
