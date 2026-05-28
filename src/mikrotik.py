"""
MikroTik RouterOS API connection helpers.

Uses a context manager so the TCP connection is always closed after use,
even when an exception is raised inside the route handler.

Failover
--------
Primary host (MIKROTIK_HOST) dicoba dulu. Bila gagal connect dalam
``_CONNECT_TIMEOUT`` detik, otomatis fallback ke MIKROTIK_HOST_BACKUP.
Login failure (password salah) tidak di-fallback — itu config error,
bukan VPN-down.

Usage
-----
    with get_mikrotik_api() as api:
        resource = api.get_resource('/ip/hotspot/user/profile')
        profiles = resource.get()
"""

import logging
from contextlib import contextmanager
from typing import Generator

from routeros_api import RouterOsApiPool
from routeros_api.api import RouterOsApi
from routeros_api.exceptions import RouterOsApiConnectionError

from src.model import MikroTikSettings

logger = logging.getLogger(__name__)

# Timeout connect ke router. Pendek supaya failover ke backup cepat
# saat VPN primary mati.
_CONNECT_TIMEOUT = 3


@contextmanager
def get_mikrotik_api() -> Generator[RouterOsApi, None, None]:
    """
    Open a MikroTik RouterOS API connection and yield it.

    Coba primary host dulu, lalu backup bila primary gagal. Pool selalu
    di-disconnect di finally block.

    Raises:
        Exception: Bila semua host (primary + backup) gagal dihubungi,
                   exception terakhir di-raise. FastAPI akan ubah jadi 500.
    """
    settings = MikroTikSettings()

    targets: list[tuple[str, int, str]] = [
        (settings.host, settings.port, "primary"),
    ]
    if settings.host_backup:
        targets.append((settings.host_backup, settings.port_backup, "backup"))

    pool: RouterOsApiPool | None = None
    api: RouterOsApi | None = None
    last_exc: Exception | None = None

    for host, port, label in targets:
        attempt_pool: RouterOsApiPool | None = None
        try:
            attempt_pool = RouterOsApiPool(
                host=host,
                username=settings.username,
                password=settings.password,
                port=port,
                plaintext_login=True,
            )
            attempt_pool.set_timeout(_CONNECT_TIMEOUT)
            api = attempt_pool.get_api()
            pool = attempt_pool
            logger.info("MikroTik connected via %s (%s:%d)", label, host, port)
            break
        except (RouterOsApiConnectionError, OSError) as exc:
            logger.warning(
                "MikroTik %s gagal connect (%s:%d): %s", label, host, port, exc
            )
            last_exc = exc
            if attempt_pool is not None:
                try:
                    attempt_pool.disconnect()
                except Exception:
                    pass

    if api is None or pool is None:
        raise last_exc or RuntimeError("Tidak ada host MikroTik yang bisa dihubungi")

    try:
        yield api
    finally:
        pool.disconnect()
