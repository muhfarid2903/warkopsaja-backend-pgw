"""Cache untuk daftar paket dari MikroTik.

Tujuan: bahkan kalau VPN/koneksi MT putus sebentar, pembeli tetap melihat
daftar paket (dari cache hangat), bukan halaman error 500. Tombol "Beli"
masih akan gagal selama MT down (perlu lookup profile id live), tapi
itu sudah ditangani dengan kode error 503 + pesan ramah di frontend.

Pola sengaja meniru ``vouchify-backend/src/helpers.get_packages_cached``:
TTL pendek 60 detik, fallback ke cache stale (atau list kosong) bila
fetch live gagal — tidak pernah raise. Pemanggil yang memang BUTUH data
live (mis. /api/purchase yg cari profile by id) tetap hit MT langsung.
"""

from __future__ import annotations

import logging
import threading
import time

from src.mikrotik import get_mikrotik_api
from src.model import ProfileResponse
from src.profile import parse_profile_name


logger = logging.getLogger(__name__)


_PROFILES_TTL = 60.0
_profiles_cache: list[ProfileResponse] | None = None
_profiles_ts: float = 0.0
_profiles_lock = threading.Lock()


def _fetch_profiles_live() -> list[ProfileResponse]:
    """Ambil daftar paket langsung dari MikroTik. Raise saat gagal."""
    with get_mikrotik_api() as api:
        resource = api.get_resource("/ip/hotspot/user/profile")
        out: list[ProfileResponse] = []
        for profile in resource.get():
            name = profile.get("name", "")
            if name.startswith("$"):
                continue
            parsed = parse_profile_name(name)
            if not parsed:
                continue
            out.append(
                ProfileResponse(
                    id=profile.get("id", ""),
                    name=parsed["name"],
                    duration=parsed["duration"],
                    price=parsed["price"],
                    rate_limit=profile.get("rate-limit"),
                )
            )
        out.sort(key=lambda p: int(p.price))
        return out


def get_profiles_cached() -> list[ProfileResponse]:
    """Return daftar paket. Pakai cache hangat kalau ada (≤ TTL).
    Saat fetch live gagal: fallback ke cache stale (atau list kosong).
    Tidak pernah raise.
    """
    global _profiles_cache, _profiles_ts
    with _profiles_lock:
        now = time.time()
        if _profiles_cache is not None and (now - _profiles_ts) < _PROFILES_TTL:
            return _profiles_cache
        try:
            fresh = _fetch_profiles_live()
            _profiles_cache = fresh
            _profiles_ts = now
            return fresh
        except Exception as exc:
            logger.warning(
                "Fetch paket dari MikroTik gagal, fallback ke cache stale (%s)", exc
            )
            return _profiles_cache or []
