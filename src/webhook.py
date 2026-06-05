"""
Paygatews webhook handler.

paygatews memanggil endpoint ini secara otomatis setelah pembayaran
disetujui admin (order → PAID), terlepas dari apakah pelanggan masih
membuka browser atau tidak.

Verifikasi signature
---------------------
paygatews mengirim header ``X-Signature`` = HMAC-SHA256(rawBody, API_KEY).
Kita WAJIB memverifikasi ini sebelum memproses notifikasi.

Body webhook (JSON):
    {
      "event": "order.paid",
      "order": { "id", "reference", "status": "PAID", "paidAt", ... },
      "payment": { "amount", "paidAt", "reference", "source" }
    }

Provisioning
------------
Setelah signature terverifikasi dan pembayaran dikonfirmasi:
1. Ambil credential (username/password/profile_id) dari database lokal
   (disimpan saat /api/purchase dipanggil).
2. Buat akun hotspot di MikroTik.
3. Tandai transaksi sebagai sukses.
"""

import hashlib
import hmac
import json
import logging

from fastapi import HTTPException, Request

from src.database import (
    get_transaction,
    mark_transaction_failed,
    mark_transaction_success,
)
from src.mikrotik import get_mikrotik_api
from src.model import PaygatewsSettings
from src.paygatews import get_paygatews_client

logger = logging.getLogger(__name__)


def _verify_signature(raw_body: bytes, api_key: str, received_signature: str) -> bool:
    """
    Verifikasi HMAC-SHA256 signature dari paygatews.

    Returns:
        True jika signature valid, False jika tidak.
    """
    expected = hmac.new(
        api_key.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature)


def _revoke_hotspot_user(tx, order_id: str, reason: str | None) -> dict:
    """
    Nonaktifkan akun hotspot MikroTik milik order yang dicabut (mis. fraud) dan
    putuskan sesi aktifnya. Idempoten: aman bila user sudah disabled/terhapus.
    """
    try:
        with get_mikrotik_api() as api:
            user_resource = api.get_resource("/ip/hotspot/user")
            matched = [u for u in user_resource.get() if u.get("name") == tx.username]
            for u in matched:
                user_resource.set(id=u.get("id"), disabled="yes")
            # Putuskan sesi yang sedang aktif agar langsung terputus, tidak
            # menunggu user logout sendiri.
            active_resource = api.get_resource("/ip/hotspot/active")
            for a in active_resource.get():
                if a.get("user") == tx.username:
                    active_resource.remove(id=a.get("id"))
        # Tandai gagal di DB lokal supaya status konsisten dengan kondisi
        # voucher yang sudah dinonaktifkan (mencegah account/create membuat
        # ulang akun untuk order yang dicabut).
        mark_transaction_failed(order_id)
        if matched:
            logger.info(
                "Webhook: voucher dicabut user=%r order_id=%r reason=%r",
                tx.username, order_id, reason,
            )
        else:
            logger.warning(
                "Webhook: revoke order_id=%r — user=%r tak ada di router "
                "(mungkin sudah dihapus)",
                order_id, tx.username,
            )
        return {"status": "ok", "action": "revoked"}
    except Exception as exc:
        logger.exception("Webhook: gagal mencabut voucher order_id=%r", order_id)
        raise HTTPException(status_code=500, detail="Failed to revoke voucher") from exc


async def handle_paygatews_notification(request: Request) -> dict:
    """
    Proses notifikasi pembayaran dari paygatews.

    Dipanggil oleh endpoint POST /api/webhook/paygatews di main.py.

    Alur:
    1. Baca raw body + verifikasi HMAC-SHA256 signature
    2. Cek event == "order.paid"
    3. Cari transaksi lokal berdasarkan reference (order_id)
    4. Jika pembayaran berhasil → buat akun hotspot di MikroTik
    5. Update status di database lokal

    Args:
        request: FastAPI Request object (body berisi webhook paygatews)

    Returns:
        Dict dengan field "status" berisi "ok" atau "ignored".

    Raises:
        HTTPException 401: Signature tidak valid.
        HTTPException 400: Field kurang atau event tidak dikenal.
        HTTPException 500: Gagal membuat akun di MikroTik.
    """
    raw_body = await request.body()

    # ── Verifikasi signature SEBELUM mem-parse body ─────────────────────
    # Jangan memproses (mem-parse JSON) input yang belum terverifikasi.
    received_sig = request.headers.get("x-signature", "")
    if not received_sig:
        raise HTTPException(status_code=401, detail="X-Signature header wajib")

    settings = PaygatewsSettings()
    if not _verify_signature(raw_body, settings.api_key, received_sig):
        logger.warning("Invalid paygatews signature")
        raise HTTPException(status_code=401, detail="Signature tidak valid")

    # ── Body terverifikasi → baru parse JSON ────────────────────────────
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Body bukan JSON valid") from exc

    # ── Cek event ───────────────────────────────────────────────────────
    event = body.get("event", "")
    if event not in ("order.paid", "order.revoked"):
        logger.info("Webhook event %r diabaikan", event)
        return {"status": "ignored", "reason": f"event {event!r} not handled"}

    order_data = body.get("order", {})
    order_id = order_data.get("reference", "")  # reference = order_id kita

    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order reference")

    tx = get_transaction(order_id)
    if not tx:
        logger.warning("Webhook: order_id=%r tidak ditemukan di DB lokal", order_id)
        return {"status": "ignored", "reason": "order not found in local DB"}

    # ── order.revoked: nonaktifkan voucher (mis. fraud) ────────────────
    if event == "order.revoked":
        return _revoke_hotspot_user(tx, order_id, body.get("reason"))

    # ── order.paid: cek idempotency lalu provisioning ──────────────────
    if tx.status == "success":
        logger.info("Webhook already processed for order_id=%r, skipping", order_id)
        return {"status": "ignored", "reason": "already processed"}

    # ── Verifikasi nominal pembayaran ──────────────────────────────────
    # Pastikan jumlah yang dibayar sama dengan harga paket yang dicatat saat
    # /api/purchase. Mencegah provisioning untuk pembayaran sebagian/keliru.
    # tx.amount == 0 berarti transaksi lama (sebelum kolom amount ada) →
    # lewati pengecekan agar kompatibel mundur.
    paid_amount = body.get("payment", {}).get("amount")
    if tx.amount and paid_amount is not None and int(paid_amount) != tx.amount:
        logger.warning(
            "Webhook: nominal tidak cocok order_id=%r (dibayar=%r, diharapkan=%r)",
            order_id, paid_amount, tx.amount,
        )
        mark_transaction_failed(order_id)
        raise HTTPException(status_code=400, detail="Nominal pembayaran tidak sesuai")

    # ── Buat akun hotspot di MikroTik ──────────────────────────────────
    try:
        with get_mikrotik_api() as api:
            # Cari nama profile berdasarkan ID yang disimpan di DB
            profile_resource = api.get_resource("/ip/hotspot/user/profile")
            profile_name = None
            for profile in profile_resource.get():
                name = profile.get("name", "")
                if profile.get("id") == tx.profile_id and not name.startswith("$"):
                    profile_name = name
                    break

            if not profile_name:
                logger.error(
                    "Profile ID %r not found on router (order_id=%r)",
                    tx.profile_id, order_id,
                )
                raise HTTPException(status_code=500, detail="Profile not found on router")

            # Buat user jika belum ada (idempotent)
            user_resource = api.get_resource("/ip/hotspot/user")
            user_exists = any(
                u.get("name") == tx.username for u in user_resource.get()
            )

            if not user_exists:
                user_resource.add(
                    name=tx.username,
                    password=tx.password,
                    profile=profile_name,
                )
                logger.info(
                    "Webhook: hotspot account created: user=%r profile=%r order_id=%r",
                    tx.username, profile_name, order_id,
                )
            else:
                logger.info("Webhook: user=%r already exists, skipping creation", tx.username)

        mark_transaction_success(order_id)

        # Kirim balik kredensial voucher ke paygatews agar tersimpan & tampil
        # di dashboard transaksi. Best-effort: akun hotspot SUDAH dibuat, jadi
        # kegagalan di sini tidak boleh menggagalkan provisioning (kita tetap
        # balas 200). Pakai order.id paygatews, bukan reference lokal.
        paygatews_order_id = order_data.get("id")
        if paygatews_order_id:
            try:
                get_paygatews_client().submit_voucher(
                    transaction_id=paygatews_order_id,
                    username=tx.username,
                    password=tx.password,
                )
                logger.info(
                    "Webhook: voucher dikirim ke paygatews (order_id=%r, paygatews id=%r)",
                    order_id, paygatews_order_id,
                )
            except Exception:
                logger.warning(
                    "Webhook: gagal kirim voucher ke paygatews (id=%r) — "
                    "provisioning tetap sukses",
                    paygatews_order_id, exc_info=True,
                )

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Webhook: failed to provision account for order_id=%r", order_id)
        raise HTTPException(status_code=500, detail="Failed to provision account") from exc
