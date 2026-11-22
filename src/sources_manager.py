"""
Verwaltung von Quellen, Kategorien und Einstellungen (sources.yaml, settings.yaml, prompts.yaml)
inklusive dauerhafter Cloud-Datenbank-Persistenz (Turso/SQLite), 3-Wege-Git-Abgleich
sowie atomarer GitHub- und CDN-Synchronisation.
"""

from __future__ import annotations

import base64
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
CONFIG_META_KEY_PREFIX: str = "config_store:"
CONFIG_SYNC_TTL_SECONDS: float = 30.0
CANONICAL_CONFIG_FILES: tuple[tuple[str, str], ...] = (
    ("sources.yaml", "config/sources.yaml"),
    ("settings.yaml", "config/settings.yaml"),
    ("prompts.yaml", "config/prompts.yaml"),
)

_CONFIG_SYNC_LOCK = threading.RLock()
_LAST_CONFIG_SYNC_TS: float = 0.0
_LAST_CONFIG_STORAGE_ID: int | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    """Persistierter YAML-Snapshot einer Konfigurationsdatei mit 3-Wege-Hash-Metadaten."""

    yaml_text: str
    sha256: str
    base_sha256: str
    updated_at: float

    def to_json(self) -> str:
        """Serialisiert den Snapshot als kompaktes JSON für die `metadata`-Tabelle."""
        return json.dumps(
            {
                "yaml": self.yaml_text,
                "sha256": self.sha256,
                "base_sha256": self.base_sha256,
                "updated_at": self.updated_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw_value: str) -> ConfigSnapshot | None:
        """Deserialisiert einen gespeicherten Snapshot sicher (Fail-Secure bei korrupten Daten)."""
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
        base_sha256 = str(payload.get("base_sha256") or "").strip()
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (ValueError, TypeError):
            updated_at = 0.0
        return cls(
            yaml_text=yaml_text,
            sha256=sha256,
            base_sha256=base_sha256,
            updated_at=updated_at,
        )


@dataclass(frozen=True)
class ConfigSaveResult:
    """Strukturiertes Ergebnis eines Konfigurations-Speichervorgangs."""

    local_saved: bool
    db_saved: bool
    github_synced: bool
    db_error: str | None = None
    github_error: str | None = None
    commit_sha: str | None = None


def _sha256_text(content: str) -> str:
    """Berechnet einen zeilenendungs-normalisierten SHA256-Hash für YAML-Inhalte."""
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Schreibt Text atomar via temporärer Datei + os.replace im selben Verzeichnis."""
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


def _read_disk_yaml_and_hash(path: Path) -> tuple[str, str]:
    """Liest den aktuellen YAML-Inhalt und Hash einer Datei (leerer String falls fehlend)."""
    if not path.exists():
        return "", ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    return content, _sha256_text(content) if content.strip() else ""


def _get_project_root() -> Path:
    """Ermittelt das Projekt-Wurzelverzeichnis."""
    return Path(__file__).resolve().parent.parent


def _is_canonical_config_path(path: Path, default_rel: str) -> bool:
    """Prüft, ob `path` auf eine kanonische Konfigurationsdatei der Anwendung zeigt."""
    canonical = (_get_project_root() / default_rel).resolve()
    try:
        return path.resolve() == canonical
    except OSError:
        return False


def invalidate_config_sync_cache() -> None:
    """Setzt den In-Process-TTL-Cache für den Cloud-DB-Konfigurationsabgleich zurück."""
    global _LAST_CONFIG_SYNC_TS, _LAST_CONFIG_STORAGE_ID
    with _CONFIG_SYNC_LOCK:
        _LAST_CONFIG_SYNC_TS = 0.0
        _LAST_CONFIG_STORAGE_ID = None


def sync_configs_from_storage(
    force: bool = False,
    storage: Any | None = None,
) -> dict[str, str]:
    """
    Synchronisiert die kanonischen Konfigurationsdateien (sources.yaml, settings.yaml, prompts.yaml)
    per 3-Wege-Hash-Abgleich zwischen lokalem Dateisystem (Git-Stand) und Cloud-Datenbank (Turso/SQLite).

    - Unsynchronisierte UI-Änderungen in der Cloud-DB (`sha256 != base_sha256`) gewinnen stets
      gegenüber einem veralteten Git-Checkout (z. B. nach Streamlit-Cloud-Redeploy oder im Daily-Digest-Runner).
    - Direkte Git-Commits (`disk_hash != base_sha256` bei synchroner DB `sha256 == base_sha256`)
      werden sauber übernommen und in die DB gespiegelt.
    """
    global _LAST_CONFIG_SYNC_TS, _LAST_CONFIG_STORAGE_ID

    with _CONFIG_SYNC_LOCK:
        if storage is None:
            try:
                from src.storage import get_storage
                storage = get_storage()
            except Exception as exc:
                logger.warning("Konfigurations-Sync übersprungen (Storage nicht verfügbar): %s", exc)
                return {}

        now = time.monotonic()
        storage_id = id(storage)
        if (
            not force
            and _LAST_CONFIG_STORAGE_ID == storage_id
            and (now - _LAST_CONFIG_SYNC_TS) < CONFIG_SYNC_TTL_SECONDS
        ):
            return {}

        meta_keys = [f"{CONFIG_META_KEY_PREFIX}{fname}" for fname, _ in CANONICAL_CONFIG_FILES]
        try:
            raw_records = storage.get_metadata_many(meta_keys)
        except (StorageError, StorageConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Konfigurations-Sync aus Datenbank fehlgeschlagen (nutze lokale Dateien): %s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001 - Storage-Ausfall darf Laden lokaler YAML nicht blockieren
            logger.warning("Unerwarteter Fehler beim Konfigurations-Sync aus Datenbank: %s", exc)
            return {}

        if not raw_records:
            _LAST_CONFIG_SYNC_TS = now
            _LAST_CONFIG_STORAGE_ID = storage_id
            return {}

        statuses: dict[str, str] = {}
        db_updates: dict[str, str] = {}
        root_dir = _get_project_root()

        for fname, rel_path in CANONICAL_CONFIG_FILES:
            meta_key = f"{CONFIG_META_KEY_PREFIX}{fname}"
            raw_json = raw_records.get(meta_key)
            if not raw_json:
                statuses[fname] = "disk_only"
                continue

            snap = ConfigSnapshot.from_json(raw_json)
            if snap is None:
                logger.warning("Ungültiger Config-Snapshot in Datenbank für %s ignoriert.", fname)
                statuses[fname] = "invalid_snapshot"
                continue

            try:
                parsed_db = yaml.safe_load(snap.yaml_text)
            except yaml.YAMLError as exc:
                logger.warning("Korruptes YAML im DB-Snapshot für %s ignoriert: %s", fname, exc)
                statuses[fname] = "invalid_yaml"
                continue

            if not isinstance(parsed_db, dict):
                statuses[fname] = "invalid_yaml"
                continue

            abs_path = (root_dir / rel_path).resolve()
            disk_text, disk_hash = _read_disk_yaml_and_hash(abs_path)

            if disk_hash == snap.sha256:
                # Datei und Datenbank sind identisch; ggf. base_sha256 nachziehen (Git hat aufgeholt)
                if snap.base_sha256 != disk_hash:
                    caught_up = ConfigSnapshot(
                        yaml_text=snap.yaml_text,
                        sha256=snap.sha256,
                        base_sha256=disk_hash,
                        updated_at=snap.updated_at or time.time(),
                    )
                    db_updates[meta_key] = caught_up.to_json()
                statuses[fname] = "in_sync"
                continue

            if snap.base_sha256 and snap.sha256 == snap.base_sha256 and disk_hash:
                # Datenbank war synchron mit Git, Git wurde direkt aktualisiert -> Git-Stand übernehmen
                try:
                    parsed_disk = yaml.safe_load(disk_text)
                except yaml.YAMLError:
                    parsed_disk = None
                if isinstance(parsed_disk, dict):
                    git_snap = ConfigSnapshot(
                        yaml_text=disk_text,
                        sha256=disk_hash,
                        base_sha256=disk_hash,
                        updated_at=time.time(),
                    )
                    db_updates[meta_key] = git_snap.to_json()
                    statuses[fname] = "updated_db_from_disk"
                    continue

            # Datenbank enthält maßgebliche, noch nicht in Git angekommene Benutzeränderungen -> DB gewinnt!
            try:
                _atomic_write_text(abs_path, snap.yaml_text)
                statuses[fname] = "restored_from_db"
                logger.info("Konfiguration '%s' aus Cloud-Datenbank wiederhergestellt.", fname)
            except OSError as exc:
                logger.warning("Konnte DB-Konfiguration '%s' nicht auf Disk spiegeln: %s", fname, exc)
                statuses[fname] = "write_failed"

        if db_updates:
            try:
                storage.set_metadata_many(db_updates)
            except Exception as exc:  # noqa: BLE001 - Metadaten-Nachzug ist nicht-kritisch
                logger.debug("Konnte abgeglichene Config-Metadaten nicht aktualisieren: %s", exc)

        _LAST_CONFIG_SYNC_TS = time.monotonic()
        _LAST_CONFIG_STORAGE_ID = storage_id
        return statuses


def _persist_configs_to_storage(
    file_entries: dict[str, tuple[str, str]],
    storage: Any | None = None,
) -> tuple[bool, str | None]:
    """
    Persistiert ein oder mehrere kanonische YAML-Dateien atomar in der Cloud-Datenbank (`metadata`).
    `file_entries` mappt Dateiname (z. B. 'sources.yaml') -> (pre_write_disk_hash, new_yaml_text).
    """
    global _LAST_CONFIG_SYNC_TS, _LAST_CONFIG_STORAGE_ID

    if not file_entries:
        return True, None

    with _CONFIG_SYNC_LOCK:
        if storage is None:
            try:
                from src.storage import get_storage
                storage = get_storage()
            except Exception as exc:
                logger.error("Persistierung in Datenbank fehlgeschlagen (Storage-Init): %s", exc)
                return False, str(exc)

        meta_keys = [f"{CONFIG_META_KEY_PREFIX}{fname}" for fname in file_entries]
        try:
            existing_raw = storage.get_metadata_many(meta_keys)
        except Exception as exc:  # noqa: BLE001 - Bestehenden Stand best-effort lesen
            logger.debug("Vorherige Config-Snapshots konnten nicht gelesen werden: %s", exc)
            existing_raw = {}

        now_ts = time.time()
        to_write: dict[str, str] = {}
        for fname, (pre_disk_hash, new_yaml) in file_entries.items():
            meta_key = f"{CONFIG_META_KEY_PREFIX}{fname}"
            new_hash = _sha256_text(new_yaml)
            existing_snap = ConfigSnapshot.from_json(existing_raw.get(meta_key, ""))

            # Falls die lokale Datei bereits eine ungesyncte DB-Änderung enthielt, ursprüngliche Git-Basis behalten
            if existing_snap is not None and pre_disk_hash and existing_snap.sha256 == pre_disk_hash:
                base_hash = existing_snap.base_sha256
            else:
                base_hash = pre_disk_hash

            snap = ConfigSnapshot(
                yaml_text=new_yaml,
                sha256=new_hash,
                base_sha256=base_hash,
                updated_at=now_ts,
            )
            to_write[meta_key] = snap.to_json()

        try:
            storage.set_metadata_many(to_write)
            _LAST_CONFIG_SYNC_TS = time.monotonic()
            _LAST_CONFIG_STORAGE_ID = id(storage)
            return True, None
        except Exception as exc:  # noqa: BLE001 - Klare Fehlerrückgabe an Aufrufer
            logger.error("Fehler beim Speichern der Konfiguration in Datenbank: %s", exc)
            return False, str(exc)


def _mark_configs_synced_to_git(
    config_files: list[tuple[str, Path]],
    storage: Any | None = None,
) -> None:
    """Markiert erfolgreich zu GitHub gepushte Konfigurationsdateien in der DB als synchron (`base_sha256 == sha256`)."""
    with _CONFIG_SYNC_LOCK:
        if storage is None:
            try:
                from src.storage import get_storage
                storage = get_storage()
            except Exception as exc:
                logger.debug("Git-Sync-Markierung in DB übersprungen: %s", exc)
                return

        updates: dict[str, str] = {}
        now_ts = time.time()
        for rel_p, abs_p in config_files:
            fname = Path(rel_p).name
            if not _is_canonical_config_path(abs_p, rel_p):
                continue
            content, content_hash = _read_disk_yaml_and_hash(abs_p)
            if not content_hash:
                continue
            snap = ConfigSnapshot(
                yaml_text=content,
                sha256=content_hash,
                base_sha256=content_hash,
                updated_at=now_ts,
            )
            updates[f"{CONFIG_META_KEY_PREFIX}{fname}"] = snap.to_json()

        if updates:
            try:
                storage.set_metadata_many(updates)
            except Exception as exc:  # noqa: BLE001 - Nicht-kritisch nach erfolgreichem GitHub-Push
                logger.debug("Konnte Git-Sync-Status in DB nicht aktualisieren: %s", exc)


def get_streamlit_app_url(config_path: str = "config/settings.yaml") -> str:
    """Helper delegating to summarizer.get_streamlit_app_url."""
    from src.summarizer import get_streamlit_app_url as _get_url
    return _get_url(config_path)


def get_sources_path(config_path: str = "config/sources.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur sources.yaml-Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def get_settings_path(config_path: str = "config/settings.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur settings.yaml-Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def get_prompts_path(config_path: str = "config/prompts.yaml") -> Path:
    """Ermittelt den absoluten Pfad zur prompts.yaml-Datei."""
    path = Path(config_path)
    if not path.is_absolute():
        root_path = _get_project_root() / config_path
        if root_path.exists() or not path.exists():
            return root_path
    return path


def get_github_sync_config() -> dict[str, str]:
    """Liest GitHub Sync Konfiguration aus Umgebungsvariablen oder Streamlit Secrets."""
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
        "token": (token or "").strip(),
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


def _collect_tree_elements(
    config_files: list[tuple[str, Path]],
    include_rss_feeds: bool,
) -> list[dict[str, Any]]:
    """Sammelt alle für den atomaren Git-Data-API-Commit vorgesehenen Dateien."""
    tree_elements: list[dict[str, Any]] = []
    for rel_p, abs_p in config_files:
        if abs_p.exists():
            tree_elements.append({
                "path": rel_p,
                "mode": "100644",
                "type": "blob",
                "content": abs_p.read_text(encoding="utf-8"),
            })

    if include_rss_feeds:
        from src.rss_generator import get_static_rss_dir
        rss_dir = get_static_rss_dir()
        for xml_file in rss_dir.rglob("*.xml"):
            rel_path = xml_file.relative_to(rss_dir.parent.parent).as_posix()
            try:
                tree_elements.append({
                    "path": rel_path,
                    "mode": "100644",
                    "type": "blob",
                    "content": xml_file.read_text(encoding="utf-8"),
                })
            except OSError as exc:
                logger.debug("RSS-Datei '%s' konnte nicht gelesen werden: %s", rel_path, exc)

    return tree_elements


def _push_via_git_data_api(
    repo: str,
    branch: str,
    headers: dict[str, str],
    tree_elements: list[dict[str, Any]],
    commit_message: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Führt einen atomaren Multi-File-Commit über die GitHub Git Data API inkl. 422-Retry aus."""
    active_repo = repo
    last_error: str | None = None

    for attempt in range(2):
        ref_res = requests.get(
            f"https://api.github.com/repos/{active_repo}/git/ref/heads/{branch}",
            headers=headers,
            timeout=8,
        )
        # Bei Weiterleitung (umbenanntes Repo) kanonischen Namen auflösen, da POST/PATCH 301 nicht sicher folgen
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
            last_error = f"Git Ref Abruf HTTP {ref_res.status_code}"
            return None, last_error

        latest_commit_sha = ref_res.json().get("object", {}).get("sha")
        if not latest_commit_sha:
            return None, "Git Ref lieferte keine Commit-SHA"

        commit_info_res = requests.get(
            f"https://api.github.com/repos/{active_repo}/git/commits/{latest_commit_sha}",
            headers=headers,
            timeout=8,
        )
        if commit_info_res.status_code != 200:
            last_error = f"Git Commit-Info HTTP {commit_info_res.status_code}"
            return None, last_error

        base_tree_sha = commit_info_res.json().get("tree", {}).get("sha")
        tree_res = requests.post(
            f"https://api.github.com/repos/{active_repo}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_elements},
            timeout=12,
        )
        if tree_res.status_code not in (200, 201):
            last_error = f"Git Tree Erstellung HTTP {tree_res.status_code}"
            return None, last_error

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
            last_error = f"Git Commit Erstellung HTTP {new_commit_res.status_code}"
            return None, last_error

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
            }, None

        last_error = f"Git Ref Update HTTP {update_ref_res.status_code}"
        if update_ref_res.status_code == 422 and attempt == 0:
            logger.info("GitHub Ref-Konflikt (HTTP 422) – wiederhole atomaren Commit gegen neuen HEAD.")
            continue
        return None, last_error

    return None, last_error


def _push_via_contents_api(
    repo: str,
    branch: str,
    headers: dict[str, str],
    config_files: list[tuple[str, Path]],
    commit_message: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fallback-Push einzelner Konfigurationsdateien über die GitHub Contents API."""
    active_repo = _resolve_canonical_github_repo(repo, headers)
    last_sha: str | None = None
    errors: list[str] = []

    for rel_p, abs_p in config_files:
        if not abs_p.exists():
            continue
        get_res = requests.get(
            f"https://api.github.com/repos/{active_repo}/contents/{rel_p}?ref={branch}",
            headers=headers,
            timeout=8,
        )
        current_sha = get_res.json().get("sha") if get_res.status_code == 200 else None
        content_b64 = base64.b64encode(abs_p.read_bytes()).decode("utf-8")
        payload: dict[str, Any] = {
            "message": commit_message,
            "content": content_b64,
            "branch": branch,
        }
        if current_sha:
            payload["sha"] = current_sha

        put_res = requests.put(
            f"https://api.github.com/repos/{active_repo}/contents/{rel_p}",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if put_res.status_code in (200, 201):
            commit_sha = put_res.json().get("commit", {}).get("sha")
            if commit_sha:
                last_sha = commit_sha
            else:
                errors.append(f"{rel_p}: Keine Commit-Antwort (HTTP {put_res.status_code})")
        else:
            errors.append(f"{rel_p}: HTTP {put_res.status_code}")

    if last_sha and not errors:
        return {"success": True, "commit_sha": last_sha, "method": "contents_api", "repo": active_repo}, None
    err_detail = "; ".join(errors) if errors else "Fehler beim Aktualisieren der Konfigurationsdateien via Contents API"
    return None, err_detail


def sync_sources_to_github(
    config_dict: dict[str, Any] | None = None,
    config_path: str = "config/sources.yaml",
    commit_message: str = "chore(config): update configuration and RSS feeds via web dashboard",
    include_rss_feeds: bool = True,
) -> dict[str, Any]:
    """
    Pusht die aktuellen Konfigurationsdateien (sources.yaml, settings.yaml, prompts.yaml)
    und optional generierte static/rss/*.xml Feeds direkt per GitHub Git Data API
    in einem einzigen atomaren Commit (inkl. Redirect-Auflösung & 422-Konflikt-Retry).
    """
    gh_cfg = get_github_sync_config()
    token = gh_cfg["token"]
    if not token or token.startswith("your_"):
        return {"success": False, "error": "Kein GITHUB_TOKEN hinterlegt."}

    repo = gh_cfg["repo"]
    branch = gh_cfg["branch"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        if config_dict is not None:
            save_sources(config_dict, config_path=config_path, sync_github=False)

        config_files_to_sync = [
            ("config/sources.yaml", get_sources_path(config_path)),
            ("config/settings.yaml", get_settings_path()),
            ("config/prompts.yaml", get_prompts_path()),
        ]

        tree_elements = _collect_tree_elements(config_files_to_sync, include_rss_feeds=include_rss_feeds)

        # 1. Versuch: Atomarer Commit via Git Data API (sowohl für Config-only als auch mit RSS)
        git_api_err: str | None = None
        if tree_elements:
            try:
                api_res, git_api_err = _push_via_git_data_api(
                    repo=repo,
                    branch=branch,
                    headers=headers,
                    tree_elements=tree_elements,
                    commit_message=commit_message,
                )
                if api_res is not None:
                    _mark_configs_synced_to_git(config_files_to_sync)
                    if include_rss_feeds:
                        api_res["jsdelivr_purge"] = purge_jsdelivr_cache()
                    return api_res
            except requests.RequestException as e_tree:
                git_api_err = str(e_tree)
                logger.warning(
                    "Git Data API Multi-File-Push fehlgeschlagen, wechsle zu Contents API Fallback: %s",
                    e_tree,
                )

        # 2. Fallback: Konfigurationsdateien via Contents API aktualisieren
        contents_res, contents_err = _push_via_contents_api(
            repo=repo,
            branch=branch,
            headers=headers,
            config_files=config_files_to_sync,
            commit_message=commit_message,
        )
        if contents_res is not None:
            _mark_configs_synced_to_git(config_files_to_sync)
            if include_rss_feeds:
                contents_res["jsdelivr_purge"] = purge_jsdelivr_cache()
            return contents_res

        combined_error = contents_err or git_api_err or "GitHub-Synchronisation fehlgeschlagen."
        logger.warning("GitHub-Sync fehlgeschlagen (%s@%s): %s", repo, branch, combined_error)
        return {"success": False, "error": combined_error}

    except Exception as exc:  # noqa: BLE001 - Klare Fehlerstruktur ohne Token-Leak
        logger.warning("GitHub-Sync unerwartet fehlgeschlagen (%s@%s): %s", repo, branch, exc)
        return {"success": False, "error": str(exc)}


def trigger_rss_update_workflow() -> dict[str, Any]:
    """Triggert den GitHub Actions Workflow zur RSS-Feed-Generierung via workflow_dispatch oder repository_dispatch."""
    gh_cfg = get_github_sync_config()
    token = gh_cfg["token"]
    if not token or token.startswith("your_"):
        return {"success": False, "error": "Kein GITHUB_TOKEN hinterlegt."}

    repo = gh_cfg["repo"]
    branch = gh_cfg["branch"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Bevorzugt: workflow_dispatch auf update_rss.yaml (Standard für Fine-Grained Token mit Actions:write)
    try:
        wf_res = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/update_rss.yaml/dispatches",
            headers=headers,
            json={"ref": branch},
            timeout=10,
        )
        if wf_res.status_code in [200, 204]:
            return {"success": True, "method": "workflow_dispatch"}
    except Exception as e_wf:
        logger.debug("workflow_dispatch fehlgeschlagen, versuche repository_dispatch: %s", e_wf)

    # 2. Fallback: repository_dispatch
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
        if res.status_code in [200, 204]:
            return {"success": True, "method": "repository_dispatch"}
        return {"success": False, "error": f"Status {res.status_code}: {res.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def purge_jsdelivr_cache(
    paths: list[str] | None = None,
    repo: str | None = None,
    branch: str | None = None,
    timeout: int = 8,
) -> dict[str, Any]:
    """Leert den weltweiten Edge-Cache des jsDelivr CDNs für geänderte RSS-Dateien.

    Unterstützt sowohl gezielte Pfade als auch alle Dateien im static/rss/ Verzeichnis.
    Führt einen Batch-Purge via POST https://purge.jsdelivr.net/ durch sowie eine synchrone
    Invalidierung für die primären Feeds (briefing.xml, all.xml).
    """
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
                except Exception:
                    pass
        except Exception as e_find:
            logger.debug("Konnte lokale RSS-Pfade für Purge nicht automatisch ermitteln: %s", e_find)

    if not formatted_paths:
        formatted_paths = [
            f"/gh/{target_repo}@{target_branch}/static/rss/briefing.xml",
            f"/gh/{target_repo}@{target_branch}/static/rss/all.xml",
        ]

    results: dict[str, Any] = {"success": True, "purged_count": len(formatted_paths), "errors": []}

    # 1. Batch-Purge via POST https://purge.jsdelivr.net/
    try:
        post_resp = requests.post(
            "https://purge.jsdelivr.net/",
            json={"path": formatted_paths},
            headers={"Content-Type": "application/json", "User-Agent": "NewsBot-CDN-Purge/1.0"},
            timeout=timeout,
        )
        if post_resp.status_code in [200, 201, 202]:
            logger.debug(
                "⚡ [CDN] jsDelivr Batch-Purge erfolgreich angestoßen (%d Pfade, Status %d).",
                len(formatted_paths),
                post_resp.status_code,
            )
        else:
            logger.error("jsDelivr Batch-Purge antwortete mit Status %d: %s", post_resp.status_code, post_resp.text)
            results["errors"].append(f"Status {post_resp.status_code}")
    except Exception as e_post:
        logger.error("jsDelivr Batch-Purge fehlgeschlagen: %s", e_post, exc_info=True)
        results["errors"].append(str(e_post))

    # 2. Sofortige synchrone Edge-Invalidierung für briefing.xml & all.xml
    for key_file in ["briefing.xml", "all.xml"]:
        key_url = f"https://purge.jsdelivr.net/gh/{target_repo}@{target_branch}/static/rss/{key_file}"
        try:
            get_resp = requests.get(key_url, headers={"User-Agent": "NewsBot-CDN-Purge/1.0"}, timeout=timeout)
            if get_resp.status_code in [200, 201, 202]:
                logger.debug("⚡ [CDN] jsDelivr Edge-Cache für %s synchron geleert.", key_file)
            else:
                logger.debug("jsDelivr Synchron-Purge für %s: Status %d", key_file, get_resp.status_code)
        except Exception as e_get:
            logger.debug("jsDelivr Synchron-Purge für %s nicht möglich: %s", key_file, e_get)

    results["success"] = len(results["errors"]) == 0
    return results


def reconcile_prompt_templates(config: dict[str, Any]) -> bool:
    """Gleicht gespeicherte Prompt-Vorlagen in der Konfiguration mit den kanonischen Standards ab."""
    from src.summarizer import DEFAULT_MAIN_PROMPT_TEMPLATE, DEFAULT_DIRECTIVES

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
    """Lädt die KI-Prompt-Konfiguration aus config/prompts.yaml (abgeglichen mit Cloud-DB)."""
    path = get_prompts_path(config_path)
    if _is_canonical_config_path(path, "config/prompts.yaml"):
        sync_configs_from_storage()
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
    """Speichert Prompts persistent in config/prompts.yaml sowie in der Cloud-Datenbank."""
    path = get_prompts_path(config_path)
    is_app_config = _is_canonical_config_path(path, "config/prompts.yaml")
    _, pre_disk_hash = _read_disk_yaml_and_hash(path) if is_app_config else ("", "")

    yaml_str = yaml.dump(prompts, allow_unicode=True, sort_keys=False, default_flow_style=False)
    _atomic_write_text(path, yaml_str)

    db_saved = False
    db_error: str | None = None
    if persist_db and is_app_config:
        db_saved, db_error = _persist_configs_to_storage({"prompts.yaml": (pre_disk_hash, yaml_str)})

    gh_res: dict[str, Any] = {"success": False, "error": "Kein Token"}
    if sync_github:
        gh_res = sync_sources_to_github(
            config_dict=None,
            commit_message="chore(prompt): update custom AI prompts via dashboard",
            include_rss_feeds=False,
        )
    gh_res["db_saved"] = db_saved
    if db_error:
        gh_res["db_error"] = db_error
    return gh_res


def load_settings(config_path: str = "config/settings.yaml") -> dict[str, Any]:
    """Lädt die allgemeinen Anwendungseinstellungen aus config/settings.yaml (abgeglichen mit Cloud-DB)."""
    path = get_settings_path(config_path)
    if _is_canonical_config_path(path, "config/settings.yaml"):
        sync_configs_from_storage()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and "settings" in data and isinstance(data["settings"], dict):
        return data["settings"]
    return data if isinstance(data, dict) else {}


def save_settings(
    settings: dict[str, Any],
    config_path: str = "config/settings.yaml",
    sync_github: bool = False,
    backup_label: str = "Einstellungen gespeichert",
    create_backup: bool = True,
    persist_db: bool = True,
) -> dict[str, Any]:
    """Speichert globale Einstellungen persistent in config/settings.yaml (ohne Prompt-Keys),
    in der Cloud-Datenbank sowie als Backup-Snapshot in der settings_backups-Tabelle."""
    clean_settings = {k: v for k, v in settings.items() if k not in PROMPT_KEYS and k != "ad_keywords"}
    path = get_settings_path(config_path)
    is_app_config = _is_canonical_config_path(path, "config/settings.yaml")
    _, pre_disk_hash = _read_disk_yaml_and_hash(path) if is_app_config else ("", "")

    yaml_str = yaml.dump(clean_settings, allow_unicode=True, sort_keys=False, default_flow_style=False)
    _atomic_write_text(path, yaml_str)

    db_saved = False
    db_error: str | None = None
    if persist_db and is_app_config:
        db_saved, db_error = _persist_configs_to_storage({"settings.yaml": (pre_disk_hash, yaml_str)})

    # Automatisches Backup nur für die tatsächliche Anwendungs-Konfiguration, nicht für temporäre Testdateien
    if create_backup and is_app_config:
        try:
            from src.storage import get_storage
            storage = get_storage()
            storage.save_settings_backup(
                settings_yaml=yaml_str,
                label=backup_label,
                filter_ads=bool(clean_settings.get("filter_ads", True)),
                keywords_count=0,
                patterns_count=len(clean_settings.get("ad_patterns", [])),
            )
        except Exception as exc:
            logger.debug("Automatisches Settings-Backup konnte nicht erstellt werden: %s", exc)

    gh_res: dict[str, Any] = {"success": False, "error": "Kein Token"}
    if sync_github:
        gh_res = sync_sources_to_github(config_dict=None, include_rss_feeds=False)
    gh_res["db_saved"] = db_saved
    if db_error:
        gh_res["db_error"] = db_error
    return gh_res


def load_sources_raw(config_path: str = "config/sources.yaml") -> dict[str, Any]:
    """Lädt ausschließlich die Kategorien und Feeds aus config/sources.yaml (abgeglichen mit Cloud-DB)."""
    path = get_sources_path(config_path)
    if _is_canonical_config_path(path, "config/sources.yaml"):
        sync_configs_from_storage()
    if not path.exists():
        raise FileNotFoundError(f"Konfigurationsdatei {path} nicht gefunden.")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    if "categories" not in data:
        data["categories"] = []
    data["categories"].sort(key=lambda c: c.get("name", "").strip().lower())
    for cat in data["categories"]:
        for f in cat.get("feeds", []):
            f.pop("max_items", None)
    return data


def load_sources(config_path: str = "config/sources.yaml", auto_reconcile: bool = True) -> dict[str, Any]:
    """Lädt die Quellenkonfiguration aus sources.yaml, ergänzt um Einstellungen aus settings.yaml
    und Prompts aus prompts.yaml für nahtlose Abwärtskompatibilität."""
    data = load_sources_raw(config_path)

    path = get_sources_path(config_path)
    settings_file = path.parent / "settings.yaml"
    prompts_file = path.parent / "prompts.yaml"

    settings_data: dict[str, Any] = {}
    if settings_file.exists():
        settings_data = load_settings(str(settings_file))
    elif get_settings_path().exists():
        settings_data = load_settings()

    prompts_data: dict[str, Any] = {}
    if prompts_file.exists():
        prompts_data = load_prompts(str(prompts_file))
    elif get_prompts_path().exists():
        prompts_data = load_prompts()

    merged_settings = dict(data.get("settings", {}))
    merged_settings.update(settings_data)
    merged_settings.update(prompts_data)
    data["settings"] = merged_settings

    if auto_reconcile:
        reconcile_prompt_templates(data)

    return data


def save_sources(
    config: dict[str, Any],
    config_path: str = "config/sources.yaml",
    sync_github: bool = True,
    persist_db: bool = True,
    include_rss_feeds: bool = False,
) -> dict[str, Any]:
    """Speichert die Quellenkonfiguration persistent in sources.yaml, settings.yaml und prompts.yaml,
    persistiert alle geänderten Konfigurationsdateien atomar in der Cloud-Datenbank (1 Batch-Roundtrip)
    und synchronisiert bei Bedarf atomar mit GitHub."""
    path = get_sources_path(config_path)
    is_canonical_sources = _is_canonical_config_path(path, "config/sources.yaml")
    _, pre_sources_hash = _read_disk_yaml_and_hash(path) if is_canonical_sources else ("", "")

    sources_data: dict[str, Any] = {}
    if "categories" in config:
        categories = list(config["categories"])
        categories.sort(key=lambda c: c.get("name", "").strip().lower())
        for cat in categories:
            for f in cat.get("feeds", []):
                f.pop("max_items", None)
        sources_data["categories"] = categories
    else:
        sources_data["categories"] = []

    sources_yaml = yaml.dump(sources_data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    _atomic_write_text(path, sources_yaml)

    db_entries: dict[str, tuple[str, str]] = {}
    if is_canonical_sources:
        db_entries["sources.yaml"] = (pre_sources_hash, sources_yaml)

    settings_dict = config.get("settings")
    if isinstance(settings_dict, dict):
        settings_file = path.parent / "settings.yaml"
        prompts_file = path.parent / "prompts.yaml"

        prompt_data = {k: v for k, v in settings_dict.items() if k in PROMPT_KEYS}
        general_settings = {k: v for k, v in settings_dict.items() if k not in PROMPT_KEYS and k != "ad_keywords"}

        if general_settings:
            is_canonical_settings = _is_canonical_config_path(settings_file, "config/settings.yaml")
            _, pre_settings_hash = _read_disk_yaml_and_hash(settings_file) if is_canonical_settings else ("", "")
            save_settings(general_settings, str(settings_file), sync_github=False, persist_db=False)
            if is_canonical_settings:
                settings_yaml = yaml.dump(
                    general_settings,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                db_entries["settings.yaml"] = (pre_settings_hash, settings_yaml)

        if prompt_data:
            is_canonical_prompts = _is_canonical_config_path(prompts_file, "config/prompts.yaml")
            _, pre_prompts_hash = _read_disk_yaml_and_hash(prompts_file) if is_canonical_prompts else ("", "")
            save_prompts(prompt_data, str(prompts_file), sync_github=False, persist_db=False)
            if is_canonical_prompts:
                prompts_yaml = yaml.dump(
                    prompt_data,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                db_entries["prompts.yaml"] = (pre_prompts_hash, prompts_yaml)

    db_saved = False
    db_error: str | None = None
    if persist_db and db_entries:
        db_saved, db_error = _persist_configs_to_storage(db_entries)

    gh_res: dict[str, Any] = {"success": False, "error": "Nicht angefordert"}
    if sync_github:
        gh_res = sync_sources_to_github(
            config_dict=None,
            config_path=config_path,
            include_rss_feeds=include_rss_feeds,
        )

    gh_res["db_saved"] = db_saved
    if db_error:
        gh_res["db_error"] = db_error
    return gh_res


def save_and_sync_sources(
    config: dict[str, Any],
    *,
    config_path: str = "config/sources.yaml",
    commit_message: str = "chore(config): update configuration via web dashboard",
    include_rss_feeds: bool = False,
) -> ConfigSaveResult:
    """
    Speichert Quellen & Einstellungen lokal + atomar in der Cloud-Datenbank und führt
    genau einen sauberen GitHub-Sync durch. Liefert ein strukturiertes `ConfigSaveResult`.
    """
    save_res = save_sources(
        config,
        config_path=config_path,
        sync_github=False,
        persist_db=True,
    )
    gh_res = sync_sources_to_github(
        config_dict=None,
        config_path=config_path,
        commit_message=commit_message,
        include_rss_feeds=include_rss_feeds,
    )
    return ConfigSaveResult(
        local_saved=True,
        db_saved=bool(save_res.get("db_saved", False)),
        github_synced=bool(gh_res.get("success", False)),
        db_error=save_res.get("db_error"),
        github_error=None if gh_res.get("success") else str(gh_res.get("error") or "Unbekannter Fehler"),
        commit_sha=gh_res.get("commit_sha"),
    )


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
                offline_feeds.append({
                    "category": cat_name,
                    "name": str(f.get("name") or f.get("url") or "").strip(),
                    "url": str(f.get("url") or "").strip(),
                    "include_keywords": f.get("include_keywords", []),
                    "exclude_keywords": f.get("exclude_keywords", []),
                    "offline_download": True,
                })
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
    """
    Fügt einen neuen Feed zu einer Kategorie hinzu oder aktualisiert ihn, falls die URL bereits existiert.
    Speichert die Änderungen in sources.yaml zurück, falls save_to_disk=True.
    """
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

    existing_feed = None
    for f in feeds:
        if f.get("url", "").strip() == feed_url:
            existing_feed = f
            break

    new_feed_obj: dict[str, Any] = {
        "name": feed_name,
        "url": feed_url,
    }
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
    """Löscht einen Feed anhand seiner Kategorie und URL aus sources.yaml oder dem config-Objekt."""
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
                c for c in config["categories"]
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
    """Aktualisiert Name, URL, Keywords, Offline-Download-Status und/oder Kategorie eines bestehenden Feeds."""
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
            existing_idx = None
            for idx, tf in enumerate(target_feeds):
                if tf.get("url", "").strip() == target_url:
                    existing_idx = idx
                    break
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
    """Löscht eine komplette Kategorie inklusive aller Feeds aus sources.yaml oder dem config-Objekt."""
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
    """Aktualisiert den settings-Abschnitt in settings.yaml oder im übergebenen config-Objekt."""
    if config is not None:
        settings = config.setdefault("settings", {})
        settings.update(new_settings)
        if save_to_disk:
            save_sources(config, config_path)
        return

    # Wenn kein config-Objekt übergeben wurde: direkt settings.yaml & prompts.yaml aktualisieren
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
