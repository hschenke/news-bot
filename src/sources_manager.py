"""
Verwaltung von Quellen, Kategorien, globalen Einstellungen und KI-Prompts.

Single Source of Truth ist ausschließlich die Datenbank (Turso Cloud-DB in Produktion,
SQLite lokal). Die YAML-Dateien unter `config/` dienen lediglich als einmalige
schreibgeschützte Initial-Vorlage (Seed), falls eine komplett leere Datenbank gestartet wird.
GitHub-Sync wird ausschließlich für die statischen RSS-Feeds (`static/rss/*.xml`)
und den jsDelivr-CDN-Purge verwendet.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from src.exceptions import StorageConnectionError, StorageError
from src.filters import normalize_keywords

logger = logging.getLogger(__name__)


PROMPT_KEYS: set[str] = {"custom_main_prompt", "custom_prompt_directives"}
DEPRECATED_SETTING_KEYS: tuple[str, ...] = (
    "ad_keywords",
    "strip_ads",
    "max_article_age_weeks",
    "max_age_weeks",
    "batch_sync_interval_minutes",
    "streamlit_app_url",
)
CONFIG_META_KEY_PREFIX: str = "config_store:"
CONFIG_CACHE_TTL_SECONDS: float = 30.0
CONFIG_SYNC_TTL_SECONDS: float = CONFIG_CACHE_TTL_SECONDS
CANONICAL_CONFIG_FILES: tuple[tuple[str, str], ...] = (
    ("sources.yaml", "config/sources.yaml"),
    ("settings.yaml", "config/settings.yaml"),
    ("prompts.yaml", "config/prompts.yaml"),
)

_CONFIG_CACHE_LOCK = threading.RLock()
_CONFIG_CACHE: dict[str, dict[str, Any]] = {}
_CONFIG_CACHE_TS: float = 0.0
_CONFIG_CACHE_STORAGE_ID: int | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    """Persistierter YAML-Snapshot einer Konfiguration in der Datenbank (`metadata`)."""

    yaml_text: str
    sha256: str
    updated_at: float

    def to_json(self) -> str:
        """Serialisiert den Snapshot als kompaktes JSON für die `metadata`-Tabelle."""
        return json.dumps(
            {
                "yaml": self.yaml_text,
                "sha256": self.sha256,
                "updated_at": self.updated_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw_value: str) -> ConfigSnapshot | None:
        """Deserialisiert einen DB-Snapshot sicher (Fail-Secure & abwärtskompatibel zu Legacy-Einträgen)."""
        if not raw_value or not raw_value.strip():
            return None
        try:
            payload = json.loads(raw_value)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        yaml_text = payload.get("yaml")
        if not isinstance(yaml_text, str) or not yaml_text.strip():
            return None
        computed_sha = _sha256_text(yaml_text)
        sha256 = str(payload.get("sha256") or computed_sha).strip() or computed_sha
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (ValueError, TypeError):
            updated_at = 0.0
        return cls(yaml_text=yaml_text, sha256=sha256, updated_at=updated_at)


def _sha256_text(content: str) -> str:
    """Berechnet einen zeilenendungs-normalisierten SHA256-Hash für YAML-Inhalte."""
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _dump_yaml(data: dict[str, Any]) -> str:
    """Serialisiert ein Konfigurations-Dictionary deterministisch als UTF-8-YAML."""
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _atomic_write_text(path: Path, content: str) -> None:
    """Schreibt Text atomar via temporärer Datei + os.replace (nur für explizite Dateipfade/Tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = Path(tmp_file.name)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _get_project_root() -> Path:
    """Ermittelt das Projekt-Wurzelverzeichnis."""
    return Path(__file__).resolve().parent.parent


def _is_canonical_config_path(path: Path, default_rel: str) -> bool:
    """Prüft, ob `path` auf die kanonische Anwendungs-Konfiguration zeigt (vs. temporäre Testdateien)."""
    canonical = (_get_project_root() / default_rel).resolve()
    try:
        return path.resolve() == canonical
    except OSError:
        return False


def _detect_storage_backend(storage: Any) -> tuple[str, bool]:
    """Bestimmt den Backend-Namen ('turso' vs 'sqlite') und ob es sich um echte Cloud-Persistenz handelt."""
    from src.storage.sqlite import SqliteStorage
    from src.storage.turso import TursoStorage

    if isinstance(storage, TursoStorage):
        return "turso", True
    if isinstance(storage, SqliteStorage):
        return "sqlite", False
    return type(storage).__name__.lower(), False


def _normalize_sources_dict(raw_data: Any) -> dict[str, Any]:
    """Normalisiert Kategorien und Feeds alphabetisch und entfernt veraltete Keys."""
    data = copy.deepcopy(raw_data) if isinstance(raw_data, dict) else {}
    categories = data.get("categories")
    if not isinstance(categories, list):
        categories = []
    categories.sort(key=lambda c: str(c.get("name", "")).strip().lower())
    for cat in categories:
        feeds = cat.get("feeds")
        if isinstance(feeds, list):
            for feed in feeds:
                feed.pop("max_items", None)
    return {"categories": categories}


def _normalize_settings_dict(raw_data: Any) -> dict[str, Any]:
    """Normalisiert globale Einstellungen ohne Prompt- oder veraltete Keys."""
    if isinstance(raw_data, dict) and isinstance(raw_data.get("settings"), dict):
        source = raw_data["settings"]
    elif isinstance(raw_data, dict):
        source = raw_data
    else:
        source = {}
    return {
        k: copy.deepcopy(v)
        for k, v in source.items()
        if k not in PROMPT_KEYS and k not in DEPRECATED_SETTING_KEYS
    }


def _normalize_prompts_dict(raw_data: Any) -> dict[str, Any]:
    """Normalisiert KI-Prompts aus einem Dictionary."""
    if not isinstance(raw_data, dict):
        return {}
    return {k: copy.deepcopy(v) for k, v in raw_data.items() if k in PROMPT_KEYS or isinstance(v, str)}


def _normalize_config_by_name(fname: str, raw_data: Any) -> dict[str, Any]:
    """Wendet die passende Normalisierung je nach kanonischem Konfigurationsnamen an."""
    if fname == "sources.yaml":
        return _normalize_sources_dict(raw_data)
    if fname == "settings.yaml":
        return _normalize_settings_dict(raw_data)
    if fname == "prompts.yaml":
        return _normalize_prompts_dict(raw_data)
    return dict(raw_data) if isinstance(raw_data, dict) else {}


def _read_seed_config_from_disk(rel_path: str, fname: str) -> tuple[dict[str, Any], str]:
    """Liest die schreibgeschützte Initial-Vorlage (Seed) aus `config/`, falls vorhanden."""
    seed_path = (_get_project_root() / rel_path).resolve()
    if not seed_path.exists():
        default_data = _normalize_config_by_name(fname, {})
        return default_data, _dump_yaml(default_data)
    try:
        raw_text = seed_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw_text) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Konnte Seed-Vorlage '%s' nicht lesen: %s", rel_path, exc)
        default_data = _normalize_config_by_name(fname, {})
        return default_data, _dump_yaml(default_data)

    normalized = _normalize_config_by_name(fname, parsed)
    return normalized, _dump_yaml(normalized)


def invalidate_config_cache() -> None:
    """Leert den In-Process-Cache für Datenbank-Konfigurationen."""
    global _CONFIG_CACHE, _CONFIG_CACHE_TS, _CONFIG_CACHE_STORAGE_ID
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE = {}
        _CONFIG_CACHE_TS = 0.0
        _CONFIG_CACHE_STORAGE_ID = None


# Abwärtskompatibler Alias
invalidate_config_sync_cache = invalidate_config_cache


def _load_db_configs(
    force: bool = False,
    storage: Any | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """
    Lädt alle 3 kanonischen Konfigurationen (sources, settings, prompts) direkt aus der Datenbank
    in einem einzigen Batch-Roundtrip mit 30s In-Memory-Cache.
    Fehlt ein Eintrag in einer komplett neuen Datenbank, wird er einmalig aus `config/*.yaml` geseeded.
    """
    global _CONFIG_CACHE, _CONFIG_CACHE_TS, _CONFIG_CACHE_STORAGE_ID

    with _CONFIG_CACHE_LOCK:
        if storage is None:
            try:
                from src.storage import get_storage
                storage = get_storage()
            except Exception as exc:
                logger.warning("Storage beim Laden der Konfiguration nicht verfügbar: %s", exc)
                if _CONFIG_CACHE:
                    return copy.deepcopy(_CONFIG_CACHE), {fname: "cached_fallback" for fname, _ in CANONICAL_CONFIG_FILES}
                fallback = {fname: _read_seed_config_from_disk(rel, fname)[0] for fname, rel in CANONICAL_CONFIG_FILES}
                return fallback, {fname: "seed_fallback" for fname, _ in CANONICAL_CONFIG_FILES}

        now = time.monotonic()
        storage_id = id(storage)
        if (
            not force
            and _CONFIG_CACHE
            and _CONFIG_CACHE_STORAGE_ID == storage_id
            and (now - _CONFIG_CACHE_TS) < CONFIG_CACHE_TTL_SECONDS
        ):
            return copy.deepcopy(_CONFIG_CACHE), {fname: "cached" for fname, _ in CANONICAL_CONFIG_FILES}

        meta_keys = [f"{CONFIG_META_KEY_PREFIX}{fname}" for fname, _ in CANONICAL_CONFIG_FILES]
        try:
            raw_records = storage.get_metadata_many(meta_keys)
        except (StorageError, StorageConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Konfiguration konnte nicht aus Datenbank gelesen werden: %s", exc)
            if _CONFIG_CACHE and _CONFIG_CACHE_STORAGE_ID == storage_id:
                return copy.deepcopy(_CONFIG_CACHE), {fname: "cached_fallback" for fname, _ in CANONICAL_CONFIG_FILES}
            fallback = {fname: _read_seed_config_from_disk(rel, fname)[0] for fname, rel in CANONICAL_CONFIG_FILES}
            return fallback, {fname: "seed_fallback" for fname, _ in CANONICAL_CONFIG_FILES}

        parsed_configs: dict[str, dict[str, Any]] = {}
        statuses: dict[str, str] = {}
        to_seed: dict[str, str] = {}
        now_wall = time.time()

        for fname, rel_path in CANONICAL_CONFIG_FILES:
            meta_key = f"{CONFIG_META_KEY_PREFIX}{fname}"
            raw_json = raw_records.get(meta_key)
            snap = ConfigSnapshot.from_json(raw_json or "")

            if snap is not None:
                try:
                    parsed_db = yaml.safe_load(snap.yaml_text)
                except yaml.YAMLError as exc:
                    logger.warning("Korruptes YAML in DB-Snapshot für %s ignoriert: %s", fname, exc)
                    parsed_db = None

                if isinstance(parsed_db, dict):
                    parsed_configs[fname] = _normalize_config_by_name(fname, parsed_db)
                    statuses[fname] = "from_db"
                    continue

            # Einmaliges Seed aus lokaler Standard-Vorlage für neue/leere Datenbanken
            seed_dict, seed_yaml = _read_seed_config_from_disk(rel_path, fname)
            parsed_configs[fname] = seed_dict
            if not raw_json:
                seed_snap = ConfigSnapshot(
                    yaml_text=seed_yaml,
                    sha256=_sha256_text(seed_yaml),
                    updated_at=now_wall,
                )
                to_seed[meta_key] = seed_snap.to_json()
                statuses[fname] = "seeded_to_db"
            else:
                statuses[fname] = "invalid_db_fallback"

        if to_seed:
            try:
                storage.set_metadata_many(to_seed)
            except Exception as exc:  # noqa: BLE001 - Best-effort Seed bei schreibgeschützter/offline DB
                logger.debug("Initiales DB-Seeding übersprungen: %s", exc)

        _CONFIG_CACHE = copy.deepcopy(parsed_configs)
        _CONFIG_CACHE_TS = time.monotonic()
        _CONFIG_CACHE_STORAGE_ID = storage_id
        return copy.deepcopy(parsed_configs), statuses


def ensure_db_configs_seeded(
    force: bool = False,
    storage: Any | None = None,
) -> dict[str, str]:
    """
    Stellt sicher, dass die Datenbank-Konfiguration geladen (und bei leerer DB einmalig geseeded) ist.
    Schreibt niemals auf lokale `config/*.yaml`-Dateien zurück und startet keine Git-Subprozesse.
    """
    _, statuses = _load_db_configs(force=force, storage=storage)
    return statuses


# Abwärtskompatibler Alias
sync_configs_from_storage = ensure_db_configs_seeded


def _persist_configs_to_storage(
    yaml_by_name: dict[str, str],
    storage: Any | None = None,
) -> dict[str, Any]:
    """
    Persistiert eine oder mehrere kanonische Konfigurationen atomar in der Datenbank (1 Batch-Write)
    inklusive strikter Fail-Closed-Prüfung:
    - Wenn `TURSO_URL` + `TURSO_KEY` konfiguriert sind, wird ein lokaler SQLite-Fallback strikt abgelehnt!
    - Nach dem Schreiben erfolgt eine verbindliche Read-Back-Verifikation (SHA256 + YAML-Parse-Check).
    - `success=True` wird AUSSCHLIESSLICH geliefert, wenn alles verifiziert in der Zieldatenbank liegt!
    """
    global _CONFIG_CACHE, _CONFIG_CACHE_TS, _CONFIG_CACHE_STORAGE_ID

    if not yaml_by_name:
        return {
            "success": True,
            "db_saved": True,
            "db_verified": True,
            "is_cloud_db": False,
            "storage_backend": "none",
            "error": None,
            "db_error": None,
        }

    with _CONFIG_CACHE_LOCK:
        from src.storage import get_storage, get_turso_config

        turso_url, turso_key = get_turso_config()
        cloud_required = bool(turso_url and turso_key)
        explicit_storage = storage is not None

        if storage is None:
            try:
                storage = get_storage(force_reconnect=True)
            except Exception as exc:
                err_msg = f"Datenbankverbindung fehlgeschlagen: {exc}"
                logger.error("%s", err_msg)
                invalidate_config_cache()
                return {
                    "success": False,
                    "db_saved": False,
                    "db_verified": False,
                    "is_cloud_db": False,
                    "storage_backend": "none",
                    "error": err_msg,
                    "db_error": err_msg,
                }

        backend_name, is_cloud = _detect_storage_backend(storage)
        if cloud_required and not explicit_storage and not is_cloud:
            err_msg = "Turso Cloud-Datenbank nicht erreichbar (lokaler SQLite-Fallback wird nicht akzeptiert)."
            logger.error("%s", err_msg)
            invalidate_config_cache()
            return {
                "success": False,
                "db_saved": False,
                "db_verified": False,
                "is_cloud_db": False,
                "storage_backend": backend_name,
                "error": err_msg,
                "db_error": err_msg,
            }

        now_ts = time.time()
        meta_keys: list[str] = []
        expected_hashes: dict[str, str] = {}
        to_write: dict[str, str] = {}
        parsed_new: dict[str, dict[str, Any]] = {}

        for fname, yaml_text in yaml_by_name.items():
            try:
                parsed_obj = yaml.safe_load(yaml_text)
            except yaml.YAMLError as exc:
                err_msg = f"Ungültiges YAML für {fname}: {exc}"
                logger.error("%s", err_msg)
                return {
                    "success": False,
                    "db_saved": False,
                    "db_verified": False,
                    "is_cloud_db": is_cloud,
                    "storage_backend": backend_name,
                    "error": err_msg,
                    "db_error": err_msg,
                }
            if not isinstance(parsed_obj, dict):
                err_msg = f"Ungültige Konfigurationsstruktur für {fname}."
                return {
                    "success": False,
                    "db_saved": False,
                    "db_verified": False,
                    "is_cloud_db": is_cloud,
                    "storage_backend": backend_name,
                    "error": err_msg,
                    "db_error": err_msg,
                }

            meta_key = f"{CONFIG_META_KEY_PREFIX}{fname}"
            new_hash = _sha256_text(yaml_text)
            meta_keys.append(meta_key)
            expected_hashes[meta_key] = new_hash
            parsed_new[fname] = _normalize_config_by_name(fname, parsed_obj)
            snap = ConfigSnapshot(yaml_text=yaml_text, sha256=new_hash, updated_at=now_ts)
            to_write[meta_key] = snap.to_json()

        try:
            storage.set_metadata_many(to_write)

            # Verbindliche Read-Back-Verifikation aus der Datenbank
            verified_raw = storage.get_metadata_many(meta_keys)
            for meta_key, expected_sha in expected_hashes.items():
                read_snap = ConfigSnapshot.from_json(verified_raw.get(meta_key, ""))
                if read_snap is None or read_snap.sha256 != expected_sha:
                    err_msg = "Datenbank-Verifikation (Read-Back) fehlgeschlagen."
                    logger.error("%s (Backend: %s, Key: %s)", err_msg, backend_name, meta_key)
                    invalidate_config_cache()
                    return {
                        "success": False,
                        "db_saved": False,
                        "db_verified": False,
                        "is_cloud_db": is_cloud,
                        "storage_backend": backend_name,
                        "error": err_msg,
                        "db_error": err_msg,
                    }
                try:
                    verified_obj = yaml.safe_load(read_snap.yaml_text)
                except yaml.YAMLError:
                    verified_obj = None
                if not isinstance(verified_obj, dict):
                    err_msg = "Datenbank-Verifikation (Read-Back YAML) fehlgeschlagen."
                    logger.error("%s (Backend: %s, Key: %s)", err_msg, backend_name, meta_key)
                    invalidate_config_cache()
                    return {
                        "success": False,
                        "db_saved": False,
                        "db_verified": False,
                        "is_cloud_db": is_cloud,
                        "storage_backend": backend_name,
                        "error": err_msg,
                        "db_error": err_msg,
                    }

            # Cache atomar auf den verifizierten Stand aktualisieren
            if _CONFIG_CACHE_STORAGE_ID == id(storage) and _CONFIG_CACHE:
                _CONFIG_CACHE.update(copy.deepcopy(parsed_new))
                _CONFIG_CACHE_TS = time.monotonic()
            else:
                invalidate_config_cache()

            return {
                "success": True,
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": is_cloud,
                "storage_backend": backend_name,
                "error": None,
                "db_error": None,
            }
        except Exception as exc:  # noqa: BLE001 - Klare Fail-Closed-Rückgabe an Aufrufer
            err_msg = f"Datenbank-Speicherfehler ({backend_name}): {exc}"
            logger.error("%s", err_msg)
            invalidate_config_cache()
            return {
                "success": False,
                "db_saved": False,
                "db_verified": False,
                "is_cloud_db": is_cloud,
                "storage_backend": backend_name,
                "error": err_msg,
                "db_error": err_msg,
            }


def get_streamlit_app_url(config_path: str = "config/settings.yaml") -> str:
    """Helper delegating to summarizer.get_streamlit_app_url."""
    from src.summarizer import get_streamlit_app_url as _get_url
    return _get_url(config_path)


def get_sources_path(config_path: str = "config/sources.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur sources.yaml-Vorlage oder einer expliziten Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def get_settings_path(config_path: str = "config/settings.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur settings.yaml-Vorlage oder einer expliziten Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def get_prompts_path(config_path: str = "config/prompts.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur prompts.yaml-Vorlage oder einer expliziten Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def _sanitize_github_token(raw_token: str | None) -> str:
    """Bereinigt einen GitHub-Token von Whitespace, Anführungszeichen und doppelten Auth-Präfixen."""
    cleaned = (raw_token or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
        cleaned = cleaned[1:-1].strip()
    lower = cleaned.lower()
    if lower.startswith("bearer "):
        cleaned = cleaned[7:].strip()
    elif lower.startswith("token "):
        cleaned = cleaned[6:].strip()
    return cleaned


def _format_github_http_error(status_code: int, context: str) -> str:
    """Erzeugt eine präzise Fehlermeldung ohne sensible Token-Daten."""
    if status_code == 401:
        return "GitHub-Token ungültig oder abgelaufen (HTTP 401) – bitte GITHUB_TOKEN in Streamlit Secrets erneuern."
    if status_code == 403:
        return "GitHub-Token ohne Schreibrechte (HTTP 403) – bitte Contents: Read & Write prüfen."
    return f"{context} (HTTP {status_code})"


def get_github_sync_config() -> dict[str, str]:
    """Liest GitHub Sync Konfiguration (für RSS-Feed-CDN-Push) aus Env oder Streamlit Secrets."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH")

    if (not token or not repo or not branch) and (
        "STREAMLIT_SERVER_PORT" in os.environ or "streamlit" in sys.modules
    ):
        try:
            import streamlit as st
            if hasattr(st, "secrets"):
                if not token:
                    if "GITHUB_TOKEN" in st.secrets:
                        token = str(st.secrets["GITHUB_TOKEN"])
                    elif "GH_TOKEN" in st.secrets:
                        token = str(st.secrets["GH_TOKEN"])
                if not repo and "GITHUB_REPO" in st.secrets:
                    repo = str(st.secrets["GITHUB_REPO"])
                if not branch and "GITHUB_BRANCH" in st.secrets:
                    branch = str(st.secrets["GITHUB_BRANCH"])
        except Exception as exc:  # noqa: BLE001 - Streamlit Secrets sind optional
            logger.debug("Streamlit Secrets für GitHub-Sync nicht lesbar: %s", type(exc).__name__)

    return {
        "token": _sanitize_github_token(token),
        "repo": (repo or "hschenke/news-bot").strip(),
        "branch": (branch or "main").strip(),
    }


def _resolve_canonical_github_repo(
    repo: str,
    headers: dict[str, str],
    timeout: int = 8,
) -> str:
    """Löst umbenannte GitHub-Repositories (z. B. news-aggregator-bot -> news-bot) über die API auf."""
    try:
        repo_res = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=headers,
            timeout=timeout,
        )
        if repo_res.status_code == 200:
            full_name = str(repo_res.json().get("full_name") or "").strip()
            if full_name and full_name.lower() != repo.lower():
                logger.info("GitHub-Repository-Weiterleitung aufgelöst: %s -> %s", repo, full_name)
                return full_name
    except requests.RequestException as exc:
        logger.debug("Repo-Auflösung für %s fehlgeschlagen: %s", repo, exc)
    return repo


def _collect_rss_tree_elements() -> list[dict[str, Any]]:
    """Sammelt alle generierten `static/rss/*.xml` Dateien für den atomaren Git-Data-API-Commit."""
    from src.rss_generator import get_static_rss_dir

    tree_elements: list[dict[str, Any]] = []
    rss_dir = get_static_rss_dir()
    if not rss_dir.exists():
        return tree_elements
    for xml_file in sorted(rss_dir.rglob("*.xml")):
        rel_path = xml_file.relative_to(rss_dir.parent.parent).as_posix()
        try:
            tree_elements.append(
                {
                    "path": rel_path,
                    "mode": "100644",
                    "type": "blob",
                    "content": xml_file.read_text(encoding="utf-8"),
                }
            )
        except OSError as exc:
            logger.debug("RSS-Datei '%s' konnte nicht gelesen werden: %s", rel_path, exc)
    return tree_elements


def _push_via_git_data_api(
    repo: str,
    branch: str,
    headers: dict[str, str],
    tree_elements: list[dict[str, Any]],
    commit_message: str,
) -> tuple[dict[str, Any] | None, str | None, int | None]:
    """Führt einen atomaren Multi-File-Commit über die GitHub Git Data API inkl. 422-Retry aus."""
    active_repo = repo
    last_error: str | None = None
    last_status: int | None = None

    for attempt in range(2):
        ref_res = requests.get(
            f"https://api.github.com/repos/{active_repo}/git/ref/heads/{branch}",
            headers=headers,
            timeout=8,
        )
        if ref_res.status_code in (401, 403):
            return None, _format_github_http_error(ref_res.status_code, "Git Ref Abruf"), ref_res.status_code

        if getattr(ref_res, "history", None) or ref_res.status_code in (301, 302, 307, 308, 404):
            resolved_repo = _resolve_canonical_github_repo(active_repo, headers)
            if resolved_repo != active_repo:
                active_repo = resolved_repo
                ref_res = requests.get(
                    f"https://api.github.com/repos/{active_repo}/git/ref/heads/{branch}",
                    headers=headers,
                    timeout=8,
                )

        if ref_res.status_code != 200:
            last_status = ref_res.status_code
            last_error = _format_github_http_error(ref_res.status_code, "Git Ref Abruf")
            return None, last_error, last_status

        latest_commit_sha = ref_res.json().get("object", {}).get("sha")
        if not latest_commit_sha:
            return None, "Git Ref lieferte keine Commit-SHA", 200

        commit_info_res = requests.get(
            f"https://api.github.com/repos/{active_repo}/git/commits/{latest_commit_sha}",
            headers=headers,
            timeout=8,
        )
        if commit_info_res.status_code != 200:
            last_status = commit_info_res.status_code
            last_error = _format_github_http_error(commit_info_res.status_code, "Git Commit-Info")
            return None, last_error, last_status

        base_tree_sha = commit_info_res.json().get("tree", {}).get("sha")
        tree_res = requests.post(
            f"https://api.github.com/repos/{active_repo}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_elements},
            timeout=12,
        )
        if tree_res.status_code not in (200, 201):
            last_status = tree_res.status_code
            last_error = _format_github_http_error(tree_res.status_code, "Git Tree Erstellung")
            return None, last_error, last_status

        new_tree_sha = tree_res.json().get("sha")
        new_commit_res = requests.post(
            f"https://api.github.com/repos/{active_repo}/git/commits",
            headers=headers,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [latest_commit_sha],
            },
            timeout=10,
        )
        if new_commit_res.status_code not in (200, 201):
            last_status = new_commit_res.status_code
            last_error = _format_github_http_error(new_commit_res.status_code, "Git Commit Erstellung")
            return None, last_error, last_status

        new_commit_sha = new_commit_res.json().get("sha")
        update_ref_res = requests.patch(
            f"https://api.github.com/repos/{active_repo}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": new_commit_sha, "force": False},
            timeout=10,
        )
        if update_ref_res.status_code == 200:
            return {
                "success": True,
                "commit_sha": new_commit_sha,
                "method": "git_data_api_multi_file",
                "files_count": len(tree_elements),
                "repo": active_repo,
                "status_code": 200,
            }, None, 200

        last_status = update_ref_res.status_code
        last_error = _format_github_http_error(update_ref_res.status_code, "Git Ref Update")
        if update_ref_res.status_code == 422 and attempt == 0:
            logger.info("GitHub Ref-Konflikt (HTTP 422) – wiederhole atomaren Commit gegen neuen HEAD.")
            continue
        return None, last_error, last_status

    return None, last_error, last_status


def sync_sources_to_github(
    config_dict: dict[str, Any] | None = None,
    config_path: str = "config/sources.yaml",
    commit_message: str = "chore(rss): update static RSS feeds via web dashboard",
    include_rss_feeds: bool = True,
) -> dict[str, Any]:
    """
    Pusht generierte `static/rss/*.xml` Feeds atomar per GitHub Git Data API zu GitHub
    und leert anschließend den jsDelivr-CDN-Cache.
    (Konfigurationsdaten liegen ausschließlich in der Datenbank und werden nicht mehr nach Git gepusht.)
    """
    gh_cfg = get_github_sync_config()
    token = gh_cfg["token"]
    if not token or token.startswith("your_"):
        return {"success": False, "error": "Kein GITHUB_TOKEN hinterlegt.", "status_code": None}

    repo = gh_cfg["repo"]
    branch = gh_cfg["branch"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        if config_dict is not None:
            db_res = save_sources(config_dict, config_path=config_path, sync_github=False, persist_db=True)
            if not db_res.get("success"):
                return {
                    "success": False,
                    "error": db_res.get("error") or "Datenbank-Speicherung fehlgeschlagen.",
                    "status_code": None,
                }

        tree_elements = _collect_rss_tree_elements() if include_rss_feeds else []
        if not tree_elements:
            return {
                "success": False,
                "error": "Keine RSS-Feed-Dateien zum Synchronisieren gefunden.",
                "status_code": None,
            }

        api_res, git_api_err, git_status = _push_via_git_data_api(
            repo=repo,
            branch=branch,
            headers=headers,
            tree_elements=tree_elements,
            commit_message=commit_message,
        )
        if api_res is not None:
            api_res["jsdelivr_purge"] = purge_jsdelivr_cache()
            return api_res

        logger.warning("GitHub-RSS-Sync fehlgeschlagen (%s@%s): %s", repo, branch, git_api_err)
        return {
            "success": False,
            "error": git_api_err or "GitHub-Synchronisation fehlgeschlagen.",
            "status_code": git_status,
        }
    except requests.RequestException as exc:
        logger.warning("GitHub-RSS-Sync Netzwerkfehler (%s@%s): %s", repo, branch, exc)
        return {"success": False, "error": str(exc), "status_code": None}
    except Exception as exc:  # noqa: BLE001 - Klare Fehlerstruktur ohne Token-Leak
        logger.warning("GitHub-RSS-Sync unerwartet fehlgeschlagen (%s@%s): %s", repo, branch, exc)
        return {"success": False, "error": str(exc), "status_code": None}


def trigger_rss_update_workflow() -> dict[str, Any]:
    """Triggert den GitHub Actions Workflow zur RSS-Feed-Generierung via workflow_dispatch oder repository_dispatch."""
    gh_cfg = get_github_sync_config()
    token = gh_cfg["token"]
    if not token or token.startswith("your_"):
        return {"success": False, "error": "Kein GITHUB_TOKEN hinterlegt.", "status_code": None}

    repo = gh_cfg["repo"]
    branch = gh_cfg["branch"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        wf_res = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/update_rss.yaml/dispatches",
            headers=headers,
            json={"ref": branch},
            timeout=10,
        )
        if wf_res.status_code in (200, 204):
            return {"success": True, "method": "workflow_dispatch", "status_code": wf_res.status_code}
        if wf_res.status_code in (401, 403):
            return {
                "success": False,
                "error": _format_github_http_error(wf_res.status_code, "Workflow-Trigger"),
                "status_code": wf_res.status_code,
            }
    except requests.RequestException as e_wf:
        logger.debug("workflow_dispatch fehlgeschlagen, versuche repository_dispatch: %s", e_wf)

    payload = {
        "event_type": "update-rss-feeds",
        "client_payload": {"triggered_by": "streamlit_dashboard"},
    }
    try:
        res = requests.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if res.status_code in (200, 204):
            return {"success": True, "method": "repository_dispatch", "status_code": res.status_code}
        return {
            "success": False,
            "error": _format_github_http_error(res.status_code, "Repository-Dispatch"),
            "status_code": res.status_code,
        }
    except requests.RequestException as exc:
        return {"success": False, "error": str(exc), "status_code": None}


def purge_jsdelivr_cache(
    paths: list[str] | None = None,
    repo: str | None = None,
    branch: str | None = None,
    timeout: int = 8,
) -> dict[str, Any]:
    """Leert den weltweiten Edge-Cache des jsDelivr CDNs für geänderte RSS-Dateien."""
    gh_cfg = get_github_sync_config()
    target_repo = repo or gh_cfg.get("repo") or "hschenke/news-bot"
    target_branch = branch or gh_cfg.get("branch") or "main"

    formatted_paths: list[str] = []
    if paths:
        for p in paths:
            clean = p.lstrip("/").replace("\\", "/")
            if not clean.startswith("gh/"):
                formatted_paths.append(f"/gh/{target_repo}@{target_branch}/{clean}")
            else:
                formatted_paths.append(f"/{clean}")
    else:
        try:
            from src.rss_generator import get_static_rss_dir
            rss_dir = get_static_rss_dir()
            root_dir = rss_dir.parent.parent
            for xml_file in rss_dir.rglob("*.xml"):
                try:
                    rel = xml_file.relative_to(root_dir).as_posix()
                    formatted_paths.append(f"/gh/{target_repo}@{target_branch}/{rel}")
                except ValueError:
                    continue
        except Exception as e_find:
            logger.debug("Konnte lokale RSS-Pfade für Purge nicht ermitteln: %s", e_find)

    if not formatted_paths:
        formatted_paths = [
            f"/gh/{target_repo}@{target_branch}/static/rss/briefing.xml",
            f"/gh/{target_repo}@{target_branch}/static/rss/all.xml",
        ]

    results: dict[str, Any] = {"success": True, "purged_count": len(formatted_paths), "errors": []}

    try:
        post_resp = requests.post(
            "https://purge.jsdelivr.net/",
            json={"path": formatted_paths},
            headers={"Content-Type": "application/json", "User-Agent": "NewsBot-CDN-Purge/1.0"},
            timeout=timeout,
        )
        if post_resp.status_code not in (200, 201, 202):
            logger.error("jsDelivr Batch-Purge antwortete mit Status %d: %s", post_resp.status_code, post_resp.text)
            results["errors"].append(f"Status {post_resp.status_code}")
    except requests.RequestException as e_post:
        logger.error("jsDelivr Batch-Purge fehlgeschlagen: %s", e_post)
        results["errors"].append(str(e_post))

    for key_file in ("briefing.xml", "all.xml"):
        key_url = f"https://purge.jsdelivr.net/gh/{target_repo}@{target_branch}/static/rss/{key_file}"
        try:
            requests.get(key_url, headers={"User-Agent": "NewsBot-CDN-Purge/1.0"}, timeout=timeout)
        except requests.RequestException as e_get:
            logger.debug("jsDelivr Synchron-Purge für %s nicht möglich: %s", key_file, e_get)

    results["success"] = len(results["errors"]) == 0
    return results


def reconcile_prompt_templates(config: dict[str, Any]) -> bool:
    """Gleicht gespeicherte Prompt-Vorlagen in der Konfiguration mit den kanonischen Standards ab."""
    from src.summarizer import DEFAULT_DIRECTIVES, DEFAULT_MAIN_PROMPT_TEMPLATE

    settings = config.setdefault("settings", {})
    changed = False

    current_main = settings.get("custom_main_prompt")
    if not current_main or not str(current_main).strip():
        settings["custom_main_prompt"] = DEFAULT_MAIN_PROMPT_TEMPLATE.strip()
        changed = True

    current_directives = settings.get("custom_prompt_directives")
    if not current_directives or not str(current_directives).strip():
        settings["custom_prompt_directives"] = DEFAULT_DIRECTIVES.strip()
        changed = True

    return changed


def load_prompts(config_path: str = "config/prompts.yaml") -> dict[str, Any]:
    """Lädt die KI-Prompts direkt aus der Datenbank (oder bei explizitem Dateipfad aus YAML)."""
    path = get_prompts_path(config_path)
    if _is_canonical_config_path(path, "config/prompts.yaml"):
        db_cfgs, _ = _load_db_configs()
        return db_cfgs.get("prompts.yaml", {})
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def save_prompts(
    prompts: dict[str, Any],
    config_path: str = "config/prompts.yaml",
    sync_github: bool = False,
    persist_db: bool = True,
) -> dict[str, Any]:
    """Speichert KI-Prompts atomar & verifiziert in der Datenbank (oder bei explizitem Testpfad in Datei)."""
    del sync_github  # Konfiguration wird nicht mehr über GitHub synchronisiert
    path = get_prompts_path(config_path)
    clean_prompts = _normalize_prompts_dict(prompts)
    yaml_str = _dump_yaml(clean_prompts)

    if _is_canonical_config_path(path, "config/prompts.yaml"):
        if not persist_db:
            return {"success": True, "db_saved": False, "db_verified": False, "storage_backend": "none", "error": None}
        return _persist_configs_to_storage({"prompts.yaml": yaml_str})

    _atomic_write_text(path, yaml_str)
    return {"success": True, "db_saved": False, "db_verified": False, "is_cloud_db": False, "storage_backend": "file", "error": None}


def load_settings(config_path: str = "config/settings.yaml") -> dict[str, Any]:
    """Lädt globale Einstellungen direkt aus der Datenbank (oder bei explizitem Dateipfad aus YAML)."""
    path = get_settings_path(config_path)
    if _is_canonical_config_path(path, "config/settings.yaml"):
        db_cfgs, _ = _load_db_configs()
        return db_cfgs.get("settings.yaml", {})
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _normalize_settings_dict(data)


def _create_settings_backup(clean_settings: dict[str, Any], yaml_str: str, label: str) -> None:
    """Legt nach erfolgreicher Speicherung einen historischen Settings-Backup-Snapshot in der DB an."""
    try:
        from src.storage import get_storage
        storage = get_storage()
        storage.save_settings_backup(
            settings_yaml=yaml_str,
            label=label,
            filter_ads=bool(clean_settings.get("filter_ads", True)),
            keywords_count=0,
            patterns_count=len(clean_settings.get("ad_patterns", [])),
        )
    except Exception as exc:  # noqa: BLE001 - Historisches Backup darf erfolgreichen Save nicht abbrechen
        logger.debug("Automatisches Settings-Backup konnte nicht erstellt werden: %s", exc)


def save_settings(
    settings: dict[str, Any],
    config_path: str = "config/settings.yaml",
    sync_github: bool = False,
    backup_label: str = "Einstellungen gespeichert",
    create_backup: bool = True,
    persist_db: bool = True,
) -> dict[str, Any]:
    """Speichert globale Einstellungen atomar & verifiziert in der Datenbank (+ optionales Settings-Backup)."""
    del sync_github  # Konfiguration wird nicht mehr über GitHub synchronisiert
    clean_settings = _normalize_settings_dict(settings)
    yaml_str = _dump_yaml(clean_settings)
    path = get_settings_path(config_path)

    if _is_canonical_config_path(path, "config/settings.yaml"):
        if not persist_db:
            return {"success": True, "db_saved": False, "db_verified": False, "storage_backend": "none", "error": None}
        res = _persist_configs_to_storage({"settings.yaml": yaml_str})
        if res.get("success") and create_backup:
            _create_settings_backup(clean_settings, yaml_str, backup_label)
        return res

    _atomic_write_text(path, yaml_str)
    return {"success": True, "db_saved": False, "db_verified": False, "is_cloud_db": False, "storage_backend": "file", "error": None}


def load_sources_raw(config_path: str = "config/sources.yaml") -> dict[str, Any]:
    """Lädt Kategorien & Feeds direkt aus der Datenbank (oder bei explizitem Dateipfad aus YAML)."""
    path = get_sources_path(config_path)
    if _is_canonical_config_path(path, "config/sources.yaml"):
        db_cfgs, _ = _load_db_configs()
        return db_cfgs.get("sources.yaml", {"categories": []})
    if not path.exists():
        raise FileNotFoundError(f"Konfigurationsdatei {path} nicht gefunden.")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _normalize_sources_dict(data)


def load_sources(config_path: str = "config/sources.yaml", auto_reconcile: bool = True) -> dict[str, Any]:
    """Lädt die vollständige Konfiguration (Kategorien, Feeds, Einstellungen, Prompts) in einem Aufruf."""
    path = get_sources_path(config_path)
    if _is_canonical_config_path(path, "config/sources.yaml"):
        db_cfgs, _ = _load_db_configs()
        data = copy.deepcopy(db_cfgs.get("sources.yaml", {"categories": []}))
        merged_settings = copy.deepcopy(db_cfgs.get("settings.yaml", {}))
        merged_settings.update(copy.deepcopy(db_cfgs.get("prompts.yaml", {})))
        data["settings"] = merged_settings
    else:
        data = load_sources_raw(config_path)
        settings_file = path.parent / "settings.yaml"
        prompts_file = path.parent / "prompts.yaml"
        merged_settings = dict(data.get("settings", {}))
        if settings_file.exists():
            merged_settings.update(load_settings(str(settings_file)))
        if prompts_file.exists():
            merged_settings.update(load_prompts(str(prompts_file)))
        merged_settings.pop("streamlit_app_url", None)
        data["settings"] = merged_settings

    if auto_reconcile:
        reconcile_prompt_templates(data)
    return data


def save_sources(
    config: dict[str, Any],
    config_path: str = "config/sources.yaml",
    sync_github: bool = False,
    persist_db: bool = True,
    include_rss_feeds: bool = False,
) -> dict[str, Any]:
    """
    Speichert Quellen, Einstellungen und Prompts atomar & verifiziert in einem einzigen
    Datenbank-Batch (Turso Cloud-DB / SQLite).
    Bei explizitem nicht-kanonischem Dateipfad (z. B. Tests) wird direkt in die Zieldatei geschrieben.
    """
    del include_rss_feeds
    path = get_sources_path(config_path)
    sources_data = _normalize_sources_dict(config)
    sources_yaml = _dump_yaml(sources_data)

    settings_dict = config.get("settings")
    prompt_data: dict[str, Any] = {}
    general_settings: dict[str, Any] = {}
    if isinstance(settings_dict, dict):
        prompt_data = {k: v for k, v in settings_dict.items() if k in PROMPT_KEYS}
        general_settings = _normalize_settings_dict(settings_dict)

    if _is_canonical_config_path(path, "config/sources.yaml"):
        if not persist_db:
            return {"success": True, "db_saved": False, "db_verified": False, "storage_backend": "none", "error": None}

        prev_settings = _CONFIG_CACHE.get("settings.yaml") if _CONFIG_CACHE else None
        yaml_entries: dict[str, str] = {"sources.yaml": sources_yaml}
        settings_yaml = ""
        if general_settings:
            settings_yaml = _dump_yaml(general_settings)
            yaml_entries["settings.yaml"] = settings_yaml
        if prompt_data:
            yaml_entries["prompts.yaml"] = _dump_yaml(prompt_data)

        res = _persist_configs_to_storage(yaml_entries)
        if res.get("success") and general_settings and settings_yaml and general_settings != prev_settings:
            _create_settings_backup(general_settings, settings_yaml, "Einstellungen gespeichert")
        return res

    # Nicht-kanonischer Pfad (isolierte Tests)
    _atomic_write_text(path, sources_yaml)
    if general_settings:
        save_settings(general_settings, str(path.parent / "settings.yaml"), sync_github=False, create_backup=False, persist_db=False)
    if prompt_data:
        save_prompts(prompt_data, str(path.parent / "prompts.yaml"), sync_github=False, persist_db=False)
    del sync_github
    return {"success": True, "db_saved": False, "db_verified": False, "is_cloud_db": False, "storage_backend": "file", "error": None}


def get_offline_enabled_feeds(
    config: dict[str, Any] | None = None,
    config_path: str = "config/sources.yaml",
) -> list[dict[str, Any]]:
    """Gibt alle Feeds zurück, bei denen der Offline-Download (offline_download: True) aktiviert ist."""
    if config is None:
        config = load_sources(config_path)
    offline_feeds: list[dict[str, Any]] = []
    for cat in config.get("categories", []):
        cat_name = str(cat.get("name") or "Allgemein").strip()
        for f in cat.get("feeds", []):
            if bool(f.get("offline_download", False)):
                offline_feeds.append(
                    {
                        "category": cat_name,
                        "name": str(f.get("name") or f.get("url") or "").strip(),
                        "url": str(f.get("url") or "").strip(),
                        "include_keywords": f.get("include_keywords", []),
                        "exclude_keywords": f.get("exclude_keywords", []),
                        "offline_download": True,
                    }
                )
    return offline_feeds


def add_feed(
    category_name: str,
    feed_name: str,
    feed_url: str,
    max_items: int | None = None,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
    include_keywords: Any = None,
    exclude_keywords: Any = None,
    offline_download: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fügt einen neuen Feed zu einer Kategorie hinzu oder aktualisiert ihn bei gleicher URL."""
    del max_items, kwargs
    category_name = category_name.strip()
    feed_name = feed_name.strip()
    feed_url = feed_url.strip()

    if not category_name or not feed_name or not feed_url:
        raise ValueError("Kategorie, Feed-Name und Feed-URL dürfen nicht leer sein.")

    if config is None:
        config = load_sources(config_path)
    categories = config.setdefault("categories", [])

    target_category = None
    for cat in categories:
        if cat.get("name", "").strip().lower() == category_name.lower():
            target_category = cat
            break

    if not target_category:
        target_category = {"name": category_name, "feeds": []}
        categories.append(target_category)

    categories.sort(key=lambda c: c.get("name", "").strip().lower())
    feeds = target_category.setdefault("feeds", [])

    existing_feed = next((f for f in feeds if f.get("url", "").strip() == feed_url), None)

    new_feed_obj: dict[str, Any] = {"name": feed_name, "url": feed_url}
    norm_inc = normalize_keywords(include_keywords)
    if norm_inc:
        new_feed_obj["include_keywords"] = norm_inc
    norm_exc = normalize_keywords(exclude_keywords)
    if norm_exc:
        new_feed_obj["exclude_keywords"] = norm_exc
    if bool(offline_download):
        new_feed_obj["offline_download"] = True

    if existing_feed:
        existing_feed.update(new_feed_obj)
        if not bool(offline_download):
            existing_feed.pop("offline_download", None)
        existing_feed.pop("max_items", None)
    else:
        feeds.append(new_feed_obj)

    feeds.sort(key=lambda f: f.get("name", "").strip().lower())

    if save_to_disk:
        save_sources(config, config_path)
    return config


def delete_feed(
    category_name: str,
    feed_url: str,
    delete_empty_category: bool = False,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> bool:
    """Löscht einen Feed anhand seiner Kategorie und URL."""
    if config is None:
        config = load_sources(config_path)
    categories = config.get("categories", [])
    found = False

    for cat in categories:
        if cat.get("name", "").strip().lower() == category_name.strip().lower():
            feeds = cat.get("feeds", [])
            initial_len = len(feeds)
            cat["feeds"] = [f for f in feeds if f.get("url", "").strip() != feed_url.strip()]
            if len(cat["feeds"]) < initial_len:
                found = True
            break

    if found:
        if delete_empty_category:
            config["categories"] = [
                c
                for c in config["categories"]
                if len(c.get("feeds", [])) > 0 or c.get("name", "").strip().lower() != category_name.strip().lower()
            ]
        if save_to_disk:
            save_sources(config, config_path)

    return found


def update_feed(
    category_name: str,
    old_url: str,
    new_name: str | None = None,
    new_url: str | None = None,
    new_max_items: int | None = None,
    new_category: str | None = None,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
    include_keywords: Any = None,
    exclude_keywords: Any = None,
    offline_download: bool | None = None,
    **kwargs: Any,
) -> bool:
    """Aktualisiert Name, URL, Keywords, Offline-Download-Status und/oder Kategorie eines Feeds."""
    del new_max_items, kwargs
    if config is None:
        config = load_sources(config_path)
    categories = config.get("categories", [])
    updated = False
    feed_to_move = None
    source_cat = None
    target_feed = None

    for cat in categories:
        if cat.get("name", "").strip().lower() == category_name.strip().lower():
            for f in cat.get("feeds", []):
                if f.get("url", "").strip() == old_url.strip():
                    source_cat = cat
                    target_feed = f
                    break
            if source_cat:
                break

    if not source_cat:
        for cat in categories:
            for f in cat.get("feeds", []):
                if f.get("url", "").strip() == old_url.strip():
                    source_cat = cat
                    target_feed = f
                    break
            if source_cat:
                break

    if source_cat and target_feed:
        if new_name is not None and new_name.strip():
            target_feed["name"] = new_name.strip()
        if new_url is not None and new_url.strip():
            target_feed["url"] = new_url.strip()
        target_feed.pop("max_items", None)

        if include_keywords is not None:
            norm_inc = normalize_keywords(include_keywords)
            if norm_inc:
                target_feed["include_keywords"] = norm_inc
            else:
                target_feed.pop("include_keywords", None)

        if exclude_keywords is not None:
            norm_exc = normalize_keywords(exclude_keywords)
            if norm_exc:
                target_feed["exclude_keywords"] = norm_exc
            else:
                target_feed.pop("exclude_keywords", None)

        if offline_download is not None:
            if bool(offline_download):
                target_feed["offline_download"] = True
            else:
                target_feed.pop("offline_download", None)

        updated = True

        actual_cat_name = source_cat.get("name", "").strip()
        if new_category and new_category.strip().lower() != actual_cat_name.lower():
            feed_to_move = dict(target_feed)
            source_cat["feeds"] = [x for x in source_cat.get("feeds", []) if x is not target_feed]

    if updated:
        if feed_to_move and new_category:
            target_cat = None
            target_cat_name_clean = new_category.strip()
            for c in categories:
                if c.get("name", "").strip().lower() == target_cat_name_clean.lower():
                    target_cat = c
                    break
            if not target_cat:
                target_cat = {"name": target_cat_name_clean, "feeds": []}
                categories.append(target_cat)

            target_feeds = target_cat.setdefault("feeds", [])
            target_url = feed_to_move.get("url", "").strip()
            existing_idx = next(
                (idx for idx, tf in enumerate(target_feeds) if tf.get("url", "").strip() == target_url),
                None,
            )
            if existing_idx is not None:
                target_feeds[existing_idx] = feed_to_move
            else:
                target_feeds.append(feed_to_move)

        categories.sort(key=lambda c: c.get("name", "").strip().lower())
        for c in categories:
            c.get("feeds", []).sort(key=lambda f: f.get("name", "").strip().lower())

        if save_to_disk:
            save_sources(config, config_path)

    return updated


def rename_category(
    old_name: str,
    new_name: str,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> bool:
    """Benennt eine bestehende Kategorie um."""
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name or not new_name:
        raise ValueError("Alter und neuer Kategoriename dürfen nicht leer sein.")
    if old_name.lower() == new_name.lower():
        return True

    if config is None:
        config = load_sources(config_path)
    categories = config.get("categories", [])

    for cat in categories:
        if cat.get("name", "").strip().lower() == new_name.lower():
            raise ValueError(f"Eine Kategorie namens '{new_name}' existiert bereits.")

    found = False
    for cat in categories:
        if cat.get("name", "").strip().lower() == old_name.lower():
            cat["name"] = new_name
            found = True
            break

    if found:
        categories.sort(key=lambda c: c.get("name", "").strip().lower())
        if save_to_disk:
            save_sources(config, config_path)

    return found


def add_category(
    category_name: str,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> bool:
    """Fügt eine neue Kategorie ohne Feeds hinzu, falls sie noch nicht existiert."""
    category_name = category_name.strip()
    if not category_name:
        raise ValueError("Kategoriename darf nicht leer sein.")

    if config is None:
        config = load_sources(config_path)
    categories = config.setdefault("categories", [])

    for cat in categories:
        if cat.get("name", "").strip().lower() == category_name.lower():
            return False

    categories.append({"name": category_name, "feeds": []})
    categories.sort(key=lambda c: c.get("name", "").strip().lower())
    if save_to_disk:
        save_sources(config, config_path)
    return True


def delete_category(
    category_name: str,
    config_path: str = "config/sources.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> bool:
    """Löscht eine komplette Kategorie inklusive aller Feeds."""
    if config is None:
        config = load_sources(config_path)
    categories = config.get("categories", [])
    initial_len = len(categories)
    config["categories"] = [
        c for c in categories if c.get("name", "").strip().lower() != category_name.strip().lower()
    ]
    if len(config["categories"]) < initial_len:
        if save_to_disk:
            save_sources(config, config_path)
        return True
    return False


def update_settings(
    new_settings: dict[str, Any],
    config_path: str = "config/settings.yaml",
    config: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> None:
    """Aktualisiert den settings-Abschnitt (und optional Prompts) in der Datenbank oder im config-Objekt."""
    if config is not None:
        settings = config.setdefault("settings", {})
        settings.update(new_settings)
        if save_to_disk:
            save_sources(config, config_path)
        return

    settings_data = load_settings(config_path)
    prompts_path = Path(config_path).parent / "prompts.yaml"
    prompts_config_arg = str(prompts_path) if prompts_path.exists() else "config/prompts.yaml"
    prompts_data = load_prompts(prompts_config_arg)

    clean_settings = {k: v for k, v in new_settings.items() if k not in PROMPT_KEYS}
    prompt_updates = {k: v for k, v in new_settings.items() if k in PROMPT_KEYS}

    settings_data.update(clean_settings)
    prompts_data.update(prompt_updates)

    if save_to_disk:
        save_settings(settings_data, config_path)
        if prompt_updates:
            save_prompts(prompts_data, prompts_config_arg)
