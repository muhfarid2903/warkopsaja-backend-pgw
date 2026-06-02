"""
Local transaction log — SQLite via SQLModel.

Hanya menyimpan log transaksi sebagai catatan lokal. Sumber kebenaran
adalah paygatews (untuk status pembayaran) dan MikroTik (untuk akun user).

Schema
------
    transactions
        id                – primary key (auto increment)
        order_id          – order reference (unique)
        profile_id        – MikroTik profile ID yang dibeli
        username          – username hotspot yang akan dibuat
        password          – password hotspot (disimpan agar webhook bisa pakai)
        gateway_txn_id    – ID transaksi di paygatews
        status            – "pending" | "success" | "failed"
        created_at        – waktu transaksi dibuat
        completed_at      – waktu akun berhasil dibuat (nullable)
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class Transaction(SQLModel, table=True):
    """Satu baris = satu transaksi pembayaran."""

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: str = Field(index=True, unique=True)
    profile_id: str
    username: str
    password: str = ""  # password hotspot (dibutuhkan webhook)
    gateway_txn_id: Optional[str] = None  # ID transaksi di paygatews
    status: str = "pending"             # pending | success | failed
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Default DB di /app/data — direktori yang dimaksudkan PERSISTEN (Dockerfile
# `mkdir -p /app/data`, compose mount `./data:/app/data`). Di Coolify WAJIB ada
# Persistent Storage yang di-mount ke /app/data; tanpa itu data hilang tiap
# redeploy. Path lama "sqlite:///transactions-pgw.db" menaruh file di /app/
# (efemeral). Override penuh via env DATABASE_URL bila perlu.
_DB_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/transactions-pgw.db")
_engine = create_engine(_DB_URL, connect_args={"check_same_thread": False})


def init_db() -> None:
    """Buat tabel jika belum ada. Dipanggil saat startup."""
    SQLModel.metadata.create_all(_engine)


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def log_transaction(
    order_id: str,
    profile_id: str,
    username: str,
    password: str = "",
    gateway_txn_id: str | None = None,
) -> Transaction:
    """
    Catat transaksi baru dengan status 'pending'.

    Jika order_id sudah ada (misal frontend memanggil /api/purchase dua kali),
    kembalikan data yang sudah ada tanpa duplikat.
    """
    with Session(_engine) as session:
        existing = session.exec(
            select(Transaction).where(Transaction.order_id == order_id)
        ).first()
        if existing:
            return existing

        tx = Transaction(
            order_id=order_id,
            profile_id=profile_id,
            username=username,
            password=password,
            gateway_txn_id=gateway_txn_id,
        )
        session.add(tx)
        session.commit()
        session.refresh(tx)
        return tx


def mark_transaction_success(order_id: str) -> None:
    """Tandai transaksi sebagai berhasil dan catat waktu selesai."""
    with Session(_engine) as session:
        tx = session.exec(
            select(Transaction).where(Transaction.order_id == order_id)
        ).first()
        if tx:
            tx.status = "success"
            tx.completed_at = datetime.now(timezone.utc)
            session.add(tx)
            session.commit()


def mark_transaction_failed(order_id: str) -> None:
    """Tandai transaksi sebagai gagal."""
    with Session(_engine) as session:
        tx = session.exec(
            select(Transaction).where(Transaction.order_id == order_id)
        ).first()
        if tx:
            tx.status = "failed"
            session.add(tx)
            session.commit()


def get_transaction(order_id: str) -> Optional[Transaction]:
    """Ambil satu transaksi berdasarkan order_id atau gateway_txn_id."""
    with Session(_engine) as session:
        tx = session.exec(
            select(Transaction).where(Transaction.order_id == order_id)
        ).first()
        if tx:
            return tx
        # Fallback: cari berdasarkan gateway_txn_id
        return session.exec(
            select(Transaction).where(Transaction.gateway_txn_id == order_id)
        ).first()
