"""
MikroTik Payment Backend — Paygatews Version
=============================================
FastAPI service yang menghubungkan paygatews (payment gateway) dengan
router MikroTik untuk otomatis membuat akun hotspot setelah pembayaran.

Ini adalah versi duplikat dari warkopsaja-backend yang menggunakan
paygatews sebagai pengganti Midtrans. Kedua versi bisa berjalan bersamaan.

Alur
----
1. Frontend → GET /api/profile          → daftar paket yang tersedia
2. User pilih paket → POST /api/purchase → buat transaksi di paygatews, dapat payment_url
3. User bayar via payment page (QRIS + upload bukti)
4. Admin approve di paygatews → webhook ke POST /api/webhook/paygatews → buat akun hotspot
5. Frontend → POST /api/account/create  → (fallback) cek status & buat akun

Run lokal
---------
    uvicorn main:app --reload --port 8001
"""

import logging
import random
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Load .env sebelum apapun diinisialisasi
load_dotenv()

from src.cors import setup_cors
from src.database import init_db, log_transaction, mark_transaction_success
from src.paygatews import get_paygatews_client
from src.mikrotik import get_mikrotik_api
from src.model import (
    AccountResponse,
    GetStatusRequest,
    PaymentRequest,
    PaymentResponse,
    ProfileResponse,
)
from src.profile import parse_profile_name
from src.webhook import handle_paygatews_notification

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inisialisasi database saat startup."""
    init_db()
    logger.info("Database initialized")
    yield


# ---------------------------------------------------------------------------
# Rate limiter — batasi berdasarkan IP address
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WarkopSaja – MikroTik Payment Backend (Paygatews)",
    description=(
        "Mengelola daftar paket hotspot, pembayaran via paygatews, "
        "dan provisi akun otomatis di MikroTik."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

setup_cors(app)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def generate_credentials(length: int = 4) -> tuple[str, str]:
    """Generate random username and password using unambiguous characters only."""
    chars = "abcdefghjkmnprtuvwxy3467"
    username = "".join(random.choices(chars, k=length))
    password = "".join(random.choices(chars, k=length))
    return username, password


def _get_profile_name_by_id(api, profile_id: str) -> str:
    """
    Cari nama profile MikroTik berdasarkan ID-nya.

    Raises:
        HTTPException 404: Jika profile tidak ditemukan.
    """
    resource = api.get_resource("/ip/hotspot/user/profile")
    for profile in resource.get():
        name = profile.get("name", "")
        if profile.get("id") == profile_id and not name.startswith("$"):
            return name

    raise HTTPException(
        status_code=404,
        detail=f"Profile dengan ID {profile_id!r} tidak ditemukan",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get(
    "/api/profile",
    response_model=list[ProfileResponse],
    summary="Daftar paket hotspot",
    tags=["Katalog"],
)
@limiter.limit("30/minute")
def get_profile_list(request: Request) -> list[ProfileResponse]:
    """
    Ambil semua profile hotspot dari router MikroTik.

    Profile yang tidak mengikuti format ``N_…|D_…|P_…`` diabaikan.
    Diurutkan dari harga termurah.
    """
    try:
        with get_mikrotik_api() as api:
            resource = api.get_resource("/ip/hotspot/user/profile")
            profile_list: list[ProfileResponse] = []

            for profile in resource.get():
                name = profile.get("name", "")
                if name.startswith("$"):
                    continue

                parsed = parse_profile_name(name)
                if not parsed:
                    continue

                profile_list.append(
                    ProfileResponse(
                        id=profile.get("id", ""),
                        name=parsed["name"],
                        duration=parsed["duration"],
                        price=parsed["price"],
                        rate_limit=profile.get("rate-limit"),
                    )
                )

            profile_list.sort(key=lambda p: int(p.price))
            return profile_list

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Gagal mengambil daftar profile dari MikroTik")
        raise HTTPException(status_code=500, detail="Gagal terhubung ke router") from exc


@app.post(
    "/api/purchase",
    response_model=PaymentResponse,
    summary="Buat transaksi pembayaran via paygatews",
    tags=["Pembayaran"],
)
@limiter.limit("10/minute")
def purchase(request: Request, body: PaymentRequest) -> PaymentResponse:
    """
    Buat transaksi di paygatews untuk paket yang dipilih.

    Username dan password yang digenerate disimpan di database lokal
    sehingga bisa diambil kembali saat webhook / account/create dipanggil.
    Transaksi juga dicatat dengan status 'pending'.
    """
    try:
        with get_mikrotik_api() as api:
            profile_name = _get_profile_name_by_id(api, body.profile_id)

        parsed = parse_profile_name(profile_name)
        if not parsed or not parsed.get("price"):
            raise HTTPException(
                status_code=400,
                detail=f"Tidak bisa membaca harga dari nama profile: {profile_name!r}",
            )

        price = int(parsed["price"])
        order_id = f"ORDER-{uuid.uuid4().hex[:12].upper()}"
        username, password = generate_credentials(length=4)

        # Panggil paygatews
        client = get_paygatews_client()
        from src.model import PaygatewsSettings
        settings = PaygatewsSettings()

        gw_response = client.create_transaction(
            amount=price,
            order_id=order_id,
            description=f"WiFi {parsed['name']} ({parsed['duration']})",
            callback_url=settings.callback_url or None,
        )

        # Catat ke database lokal (termasuk password untuk webhook)
        log_transaction(
            order_id=order_id,
            profile_id=body.profile_id,
            username=username,
            password=password,
            gateway_txn_id=gw_response.get("id"),
        )

        return PaymentResponse(
            payment_url=gw_response["payment_url"],
            transaction_id=gw_response["id"],
            unique_amount=gw_response.get("unique_amount"),
        )

    except HTTPException:
        raise
    except RuntimeError as exc:
        logger.exception("Gagal membuat transaksi paygatews")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Gagal membuat transaksi paygatews")
        raise HTTPException(status_code=500, detail="Gagal membuat pembayaran") from exc


@app.post(
    "/api/account/create",
    response_model=AccountResponse,
    summary="Cek status & buat akun hotspot (fallback)",
    tags=["Pembayaran"],
)
@limiter.limit("10/minute")
def create_account(request: Request, body: GetStatusRequest) -> AccountResponse:
    """
    Cek status transaksi di paygatews dan buat akun hotspot jika sudah PAID.

    Ini adalah **fallback** — jalur utama adalah webhook paygatews.
    Dipanggil frontend setelah pembayaran selesai, untuk memastikan
    akun sudah terbuat sebelum menampilkan credential ke user.
    """
    try:
        from src.database import get_transaction

        # Cari transaksi lokal dulu — dapat gateway_txn_id
        tx = get_transaction(body.order_id)
        if not tx:
            raise HTTPException(status_code=404, detail="Order tidak ditemukan")

        if tx.status == "success" and tx.username:
            # Sudah diproses oleh webhook
            return AccountResponse(
                user=tx.username,
                password=tx.password,
                message="Akun hotspot siap digunakan",
            )

        # Cek status di paygatews
        if not tx.gateway_txn_id:
            raise HTTPException(status_code=400, detail="Transaksi belum ada di gateway")

        client = get_paygatews_client()
        gw_status = client.get_transaction_status(tx.gateway_txn_id)

        if gw_status.get("status") != "PAID":
            raise HTTPException(
                status_code=400,
                detail=f"Pembayaran belum selesai. Status: {gw_status.get('status')}",
            )

        # Sudah PAID — buat akun di MikroTik
        with get_mikrotik_api() as api:
            profile_name = _get_profile_name_by_id(api, tx.profile_id)

            user_resource = api.get_resource("/ip/hotspot/user")
            user_exists = any(u.get("name") == tx.username for u in user_resource.get())

            if not user_exists:
                user_resource.add(name=tx.username, password=tx.password, profile=profile_name)
                logger.info("Akun hotspot dibuat: user=%r profile=%r", tx.username, profile_name)
            else:
                logger.info("User %r sudah ada, lewati pembuatan", tx.username)

        mark_transaction_success(body.order_id)

        return AccountResponse(
            user=tx.username,
            password=tx.password,
            message="Akun hotspot siap digunakan",
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Gagal provisi akun untuk order_id=%r", body.order_id)
        raise HTTPException(status_code=500, detail="Gagal menyelesaikan provisi akun") from exc


@app.post(
    "/api/webhook/paygatews",
    summary="Paygatews payment notification (webhook)",
    tags=["Webhook"],
)
async def paygatews_webhook(request: Request) -> dict:
    """
    Endpoint yang dipanggil otomatis oleh paygatews setelah pembayaran
    disetujui admin (order → PAID).

    **Ini adalah jalur utama pembuatan akun** — tidak bergantung pada
    apakah pelanggan masih membuka browser atau tidak.

    Signature diverifikasi di dalam ``handle_paygatews_notification``.
    """
    return await handle_paygatews_notification(request)
