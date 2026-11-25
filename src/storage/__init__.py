"""
Storage- und Datenbankpaket für den News Bot.
Bietet ein einheitliches Repository-Interface (StorageBackend) mit zwei Implementierungen:
1. TursoStorage: Cloud-basierte LibSQL-Datenbank via HTTPS v2 Pipeline (Zero heavy dependencies).
2. SqliteStorage: Lokale SQLite-Datenbank (Standardbibliothek) als robuster Offline-/Entwicklungs-Fallback.

Inklusive automatischer Erkennung und unterbrechungsfreiem Failover (Fallback) bei Verbindungsfehlern.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
import requests  # Erforderlich für Abwärtskompatibilität und Test-Mocking

from src.models import Article, OfflineArticle
from src.exceptions import StorageError, StorageConnectionError
from src.storage.base import (
    StorageBackend,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_SQLITE_PATH,
    OFFLINE_RETENTION_DAYS,
    get_safe_db_path,
    get_turso_config,
)
from src.storage.sqlite import SqliteStorage
from src.storage.turso import TursoStorage

logger = logging.getLogger(__name__)

_STORAGE_INSTANCE: StorageBackend | None = None
_STORAGE_FALLBACK_TS: float = 0.0
_TURSO_RETRY_INTERVAL_SECONDS: float = 30.0


def reset_storage_singleton() -> None:
    """Setzt die Singleton-Instanz zurück (z. B. für Tests)."""
    global _STORAGE_INSTANCE, _STORAGE_FALLBACK_TS
    _STORAGE_INSTANCE = None
    _STORAGE_FALLBACK_TS = 0.0


def is_cloud_storage(storage: StorageBackend | None = None) -> bool:
    """Prüft, ob die aktive oder übergebene Storage-Instanz eine echte Cloud-DB (TursoStorage) ist."""
    target = storage if storage is not None else _STORAGE_INSTANCE
    return isinstance(target, TursoStorage)


def get_storage(
    prefer_turso: bool = True,
    db_path: str | Path | None = None,
    force_reconnect: bool = False,
) -> StorageBackend:
    """
    Factory-Funktion mit automatischem Fallback, Auto-Recovery und Singleton-Wiederverwendung:
    1. Wenn db_path angegeben ist (z. B. in Tests für :memory:), wird stets eine isolierte Instanz erzeugt.
    2. Wenn prefer_turso=True und TURSO_URL + TURSO_KEY vorhanden sind:
       Versucht Verbindung zu Turso. Bei Erfolg wird TursoStorage wiederverwendet.
       War zuvor ein temporärer Fallback auf SqliteStorage aktiv, wird nach Ablauf des Retry-Intervalls
       (oder bei force_reconnect=True) automatisch erneut zu Turso verbunden.
    3. Automatischer Fallback auf lokales SqliteStorage.
    """
    global _STORAGE_INSTANCE, _STORAGE_FALLBACK_TS
    if db_path is not None:
        sqlite_storage = SqliteStorage(db_path=db_path)
        sqlite_storage.init_db()
        return sqlite_storage

    if _STORAGE_INSTANCE is not None and not force_reconnect:
        if isinstance(_STORAGE_INSTANCE, TursoStorage) or not prefer_turso:
            return _STORAGE_INSTANCE
        # Bei lokalem Fallback: nur nach Ablauf des Retry-Intervalls erneut Turso prüfen
        if (time.monotonic() - _STORAGE_FALLBACK_TS) < _TURSO_RETRY_INTERVAL_SECONDS:
            return _STORAGE_INSTANCE

    turso_url, turso_key = get_turso_config()

    if prefer_turso and turso_url and turso_key:
        try:
            turso_storage = TursoStorage(database_url=turso_url, auth_token=turso_key)
            turso_storage.init_db()
            _STORAGE_INSTANCE = turso_storage
            _STORAGE_FALLBACK_TS = 0.0
            return _STORAGE_INSTANCE
        except Exception as exc:
            _STORAGE_FALLBACK_TS = time.monotonic()
            logger.error(
                "Turso-Verbindung konnte nicht initialisiert werden (%s). "
                "Automatischer Fallback auf lokale SQLite-Datenbank.",
                exc,
                exc_info=True,
            )

    if isinstance(_STORAGE_INSTANCE, SqliteStorage):
        return _STORAGE_INSTANCE

    logger.debug("Nutze lokale SQLite-Speicherung.")
    sqlite_storage = SqliteStorage(db_path=db_path)
    sqlite_storage.init_db()
    _STORAGE_INSTANCE = sqlite_storage
    _STORAGE_FALLBACK_TS = time.monotonic()
    return _STORAGE_INSTANCE


__all__ = [
    "StorageBackend",
    "SqliteStorage",
    "TursoStorage",
    "StorageError",
    "StorageConnectionError",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_SQLITE_PATH",
    "OFFLINE_RETENTION_DAYS",
    "OfflineArticle",
    "get_safe_db_path",
    "get_turso_config",
    "get_storage",
    "is_cloud_storage",
    "reset_storage_singleton",
    "requests",
]
