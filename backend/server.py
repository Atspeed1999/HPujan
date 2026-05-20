from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import hmac
import hashlib
import requests
from requests.auth import HTTPBasicAuth
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection is OPTIONAL. If motor or Mongo is unavailable, the app
# still serves payment endpoints; bookings are kept in-memory only and logged.
db = None
MONGO_ENABLED = False
try:
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get('MONGO_URL')
    db_name = os.environ.get('DB_NAME', 'homepujan_db')
    if mongo_url:
        client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
        db = client[db_name]
        MONGO_ENABLED = True
except Exception:
    # motor not installed or Mongo client creation failed — fall back silently
    db = None
    MONGO_ENABLED = False

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)

    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()

    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    if db is None:
        return []
    # Exclude MongoDB's _id field from the query results
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)

    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])

    return status_checks


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

# In-memory booking store. Used as fallback when MongoDB unreachable; also kept
# in parallel with Mongo so admin endpoints always have something to show.
_BOOKINGS_MEM: dict = {}


async def _save_booking(doc: dict) -> None:
    """Persist booking. Always writes to in-memory; best-effort to Mongo."""
    _BOOKINGS_MEM[doc['id']] = dict(doc)
    print(f"\n[BOOKING CREATED] {doc['id']} | {doc['service_name']} | "
          f"₹{doc['amount_paise']/100:.0f} | {doc['slot_iso']} | "
          f"{doc['customer'].get('name')} <{doc['customer'].get('email')}>")
    if db is None:
        return
    try:
        await db.bookings.insert_one(dict(doc))
    except Exception as e:
        logger.warning("Mongo insert skipped (%s): %s", type(e).__name__, e)


async def _update_booking(booking_id: str, order_id: str, updates: dict) -> bool:
    """Update booking by id+order_id. Returns True if a record was matched."""
    matched_mem = False
    if booking_id in _BOOKINGS_MEM and _BOOKINGS_MEM[booking_id].get('razorpay_order_id') == order_id:
        _BOOKINGS_MEM[booking_id].update(updates)
        matched_mem = True
        status = updates.get('status', '?')
        print(f"[BOOKING UPDATED] {booking_id} | status={status} | "
              f"payment_id={updates.get('razorpay_payment_id', '—')}")

    if db is None:
        return matched_mem
    try:
        result = await db.bookings.update_one(
            {"id": booking_id, "razorpay_order_id": order_id},
            {"$set": updates},
        )
        return matched_mem or result.matched_count > 0
    except Exception as e:
        logger.warning("Mongo update skipped (%s): %s", type(e).__name__, e)
        return matched_mem


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
    }
    await _save_booking(booking_doc)

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
        await _update_booking(req.booking_id, req.razorpay_order_id, {
            "status": "signature_mismatch",
            "razorpay_payment_id": req.razorpay_payment_id,
        })
        raise HTTPException(status_code=400, detail="Signature verification failed")

    matched = await _update_booking(req.booking_id, req.razorpay_order_id, {
        "status": "paid",
        "razorpay_payment_id": req.razorpay_payment_id,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    })
    if not matched:
        raise HTTPException(status_code=404, detail="Booking not found for this order")

    return {"status": "ok", "booking_id": req.booking_id}


@api_router.get("/payments/bookings")
async def list_bookings_inmemory():
    """Dev helper. Returns whatever is in the in-memory booking store."""
    return {
        "mongo_enabled": MONGO_ENABLED,
        "count": len(_BOOKINGS_MEM),
        "bookings": list(_BOOKINGS_MEM.values()),
    }


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()