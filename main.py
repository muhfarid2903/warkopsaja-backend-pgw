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
import secrets
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Load .env sebelum apapun diinisialisasi
load_dotenv()

from routeros_api.exceptions import RouterOsApiConnectionError

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
from src.profile_cache import get_profiles_cached
from src.webhook import handle_paygatews_notification

# Pesan ramah yang dibalas saat MikroTik tidak bisa dihubungi. Frontend
# mendeteksi HTTP 503 → menampilkan UI "Router sedang offline".
_MT_DOWN_DETAIL = "Router hotspot lagi tidak terhubung. Coba lagi sebentar."

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

# Karakter tak ambigu (tanpa 0/O/1/l/i/q/s/z dll.) supaya mudah dibaca pembeli.
_CRED_CHARS = "abcdefghjkmnprtuvwxy3467"


def _random_token(length: int) -> str:
    """Token acak kriptografis dari karakter tak ambigu."""
    return "".join(secrets.choice(_CRED_CHARS) for _ in range(length))


def generate_credentials(length: int = 4) -> tuple[str, str]:
    """Generate random username and password using unambiguous characters only.

    Memakai ``secrets`` (CSPRNG), bukan ``random``, karena kredensial ini
    menggerbangi akses WiFi berbayar dan tidak boleh bisa diprediksi.
    """
    return _random_token(length), _random_token(length)


def _generate_unique_username(api, length: int = 4, max_tries: int = 10) -> str:
    """
    Generate username yang dijamin belum dipakai di router.

    Ruang nama length=4 kecil (~331k), jadi tabrakan mungkin terjadi. Bila
    user dengan nama sama sudah ada, webhook/account-create akan MELEWATI
    pembuatan user → pembeli berikutnya bisa tertukar akun. Kita cegah dengan
    cek live ke router, dan menaikkan panjang token bila terus bentrok.
    """
    user_resource = api.get_resource("/ip/hotspot/user")
    existing = {u.get("name") for u in user_resource.get()}
    for attempt in range(max_tries):
        # Naikkan panjang token bila beberapa percobaan pertama bentrok.
        candidate = _random_token(length + attempt // 3)
        if candidate not in existing:
            return candidate
    raise HTTPException(
        status_code=500,
        detail="Gagal membuat username unik, coba lagi",
    )


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

@app.get("/api/health", summary="Liveness probe", tags=["Sistem"])
def health() -> dict:
    """Sederhana — tidak menyentuh MikroTik supaya cocok untuk Coolify liveness."""
    return {"status": "ok"}


@app.get(
    "/api/profile",
    response_model=list[ProfileResponse],
    summary="Daftar paket hotspot",
    tags=["Katalog"],
)
@limiter.limit("30/minute")
def get_profile_list(request: Request) -> list[ProfileResponse]:
    """
    Ambil semua profile hotspot dari router MikroTik (di-cache 60 detik).

    Saat MT putus, balikan diambil dari cache stale (tidak raise) supaya
    halaman pembeli tetap menampilkan daftar paket; hanya tombol "Beli"
    yang akan 503 selama outage. Lihat ``src.profile_cache``.
    """
    return get_profiles_cached()


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
            # Generate username yang dijamin belum ada di router (cek live)
            # selagi koneksi masih terbuka, lalu password terpisah.
            username = _generate_unique_username(api, length=4)
            _, password = generate_credentials(length=4)

        parsed = parse_profile_name(profile_name)
        if not parsed or not parsed.get("price"):
            raise HTTPException(
                status_code=400,
                detail=f"Tidak bisa membaca harga dari nama profile: {profile_name!r}",
            )

        price = int(parsed["price"])
        order_id = f"ORDER-{uuid.uuid4().hex[:12].upper()}"

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
            amount=price,
            gateway_txn_id=gw_response.get("id"),
        )

        return PaymentResponse(
            payment_url=gw_response["payment_url"],
            transaction_id=gw_response["id"],
            unique_amount=gw_response.get("unique_amount"),
        )

    except HTTPException:
        raise
    except RouterOsApiConnectionError as exc:
        logger.warning("MikroTik tidak terhubung saat purchase: %s", exc)
        raise HTTPException(status_code=503, detail=_MT_DOWN_DETAIL) from exc
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

        # Pakai tx.order_id (kolom DB) supaya WHERE clause cocok — body.order_id
        # bisa berupa gateway_txn_id karena frontend kirim transaction_id.
        mark_transaction_success(tx.order_id)

        return AccountResponse(
            user=tx.username,
            password=tx.password,
            message="Akun hotspot siap digunakan",
        )

    except HTTPException:
        raise
    except RouterOsApiConnectionError as exc:
        logger.warning("MikroTik tidak terhubung saat account/create: %s", exc)
        raise HTTPException(status_code=503, detail=_MT_DOWN_DETAIL) from exc
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
