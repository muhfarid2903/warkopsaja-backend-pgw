"""
Paygatews API client.

Menggantikan Midtrans Snap client. Menghubungi paygatews gateway untuk
membuat transaksi dan mengambil status pembayaran.

paygatews menyediakan tiga endpoint yang dipakai di sini:
  - POST /v1/transactions          → buat order + dapat payment_url
  - GET  /v1/transactions/:id       → cek status transaksi
  - POST /v1/transactions/:id/voucher → kirim balik kredensial voucher
"""

import logging
from functools import lru_cache

import httpx

from src.model import PaygatewsSettings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_settings() -> PaygatewsSettings:
    """Read and validate paygatews settings exactly once."""
    return PaygatewsSettings()


class PaygatewsClient:
    """Client untuk berkomunikasi dengan paygatews gateway."""

    def __init__(self) -> None:
        self.settings = _load_settings()
        self.base_url = self.settings.gateway_url.rstrip("/")

    def create_transaction(
        self,
        *,
        amount: int,
        order_id: str,
        description: str | None = None,
        callback_url: str | None = None,
    ) -> dict:
        """
        Panggil POST /v1/transactions di paygatews.

        Mengembalikan dict dengan: id, payment_url, unique_amount, status, dll.

        Raises:
            RuntimeError: Jika gateway menolak atau tidak bisa dihubungi.
        """
        payload: dict = {
            "amount": amount,
            "reference": order_id,
            "description": description,
        }
        if callback_url:
            payload["callback_url"] = callback_url

        try:
            resp = httpx.post(
                f"{self.base_url}/v1/transactions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.exception("Gagal menghubungi paygatews")
            raise RuntimeError(f"Gagal menghubungi gateway: {exc}") from exc

        if resp.status_code != 201:
            body = resp.text[:300]
            raise RuntimeError(
                f"Gateway menolak (HTTP {resp.status_code}): {body}"
            )

        return resp.json()

    def get_transaction_status(self, transaction_id: str) -> dict:
        """
        Panggil GET /v1/transactions/:id di paygatews.

        Mengembalikan dict dengan: id, status, amount, paid_at, dll.

        Raises:
            RuntimeError: Jika gateway menolak atau tidak bisa dihubungi.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/v1/transactions/{transaction_id}",
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gagal menghubungi gateway: {exc}") from exc

        if resp.status_code == 404:
            raise RuntimeError(f"Transaksi {transaction_id!r} tidak ditemukan")

        if resp.status_code != 200:
            body = resp.text[:300]
            raise RuntimeError(
                f"Gateway menolak (HTTP {resp.status_code}): {body}"
            )

        return resp.json()

    def submit_voucher(
        self,
        *,
        transaction_id: str,
        username: str,
        password: str,
    ) -> dict:
        """
        Panggil POST /v1/transactions/:id/voucher di paygatews.

        Mengirim balik kredensial voucher hasil provisioning (user hotspot
        MikroTik) agar tersimpan & tampil di dashboard transaksi paygatews.

        ``transaction_id`` adalah **order id paygatews** (field ``order.id``
        di body webhook), BUKAN reference/order_id lokal kita.

        Mengembalikan dict transaksi paygatews terbaru (memuat ``voucher``).

        Raises:
            RuntimeError: Jika gateway menolak atau tidak bisa dihubungi.
        """
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/transactions/{transaction_id}/voucher",
                json={"username": username, "password": password},
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gagal menghubungi gateway: {exc}") from exc

        if resp.status_code != 200:
            body = resp.text[:300]
            raise RuntimeError(
                f"Gateway menolak voucher (HTTP {resp.status_code}): {body}"
            )

        return resp.json()


def get_paygatews_client() -> PaygatewsClient:
    """
    Return a configured paygatews client.

    Settings are validated on first call; subsequent calls reuse the
    cached result.
    """
    return PaygatewsClient()
