"""
Umfassende Tests für dauerhafte Cloud-DB-Konfigurationspersistenz, 3-Wege-Git-Sync,
gehärteten GitHub-Sync (Redirect-Auflösung & 422-Retry), SSRF-Schutz und Manage-Tab-Workflows.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from src.feed_fetcher import validate_feed_url
from src.models import Article
from src.sources_manager import (
    CONFIG_META_KEY_PREFIX,
    ConfigSnapshot,
    _mark_configs_synced_to_git,
    _persist_configs_to_storage,
    _sanitize_github_token,
    _sha256_text,
    invalidate_config_sync_cache,
    load_sources,
    mark_canonical_configs_synced_to_git,
    save_sources,
    sync_configs_from_storage,
    sync_sources_to_github,
)
from src.storage import get_storage, is_cloud_storage, reset_storage_singleton
from src.storage.sqlite import SqliteStorage
from src.storage.turso import TursoStorage


class TestCloudConfigPersistenceAndSync(unittest.TestCase):
    """Prüft die 3-Wege-Synchronisation zwischen lokalem Git-Checkout und Cloud-DB."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.cfg_dir = self.root / "config"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)

        self.initial_sources = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [
                        {"name": "Feed A", "url": "https://example.com/a.xml"},
                        {"name": "Feed B", "url": "https://example.com/b.xml"},
                        {"name": "Feed C", "url": "https://example.com/c.xml"},
                    ],
                }
            ]
        }
        self.initial_settings = {
            "language": "de",
            "archive_retention_days": 8,
            "filter_ads": True,
            "ad_patterns": ["^anzeige:"],
        }
        self.initial_prompts = {
            "custom_main_prompt": "Initial Prompt {lang_name} {active_directives} {context_data}",
            "custom_prompt_directives": "Initial Directives",
        }

        self.sources_file = self.cfg_dir / "sources.yaml"
        self.settings_file = self.cfg_dir / "settings.yaml"
        self.prompts_file = self.cfg_dir / "prompts.yaml"

        self.sources_file.write_text(yaml.dump(self.initial_sources, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.settings_file.write_text(yaml.dump(self.initial_settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.prompts_file.write_text(yaml.dump(self.initial_prompts, allow_unicode=True, sort_keys=False), encoding="utf-8")

        self.storage = SqliteStorage(":memory:")
        self.storage.init_db()
        invalidate_config_sync_cache()

    def tearDown(self) -> None:
        invalidate_config_sync_cache()
        self.storage.close()
        self.tmp_dir.cleanup()

    def test_user_edits_survive_git_redeploy_and_daily_briefing_without_github_token(self) -> None:
        """
        Exakter Regressionstest für den gemeldeten Fehler:
        2 Feeds löschen + 1 neuen Feed hinzufügen + Einstellungen ändern -> speichern (GitHub-Sync schlägt fehl)
        -> Streamlit-Redeploy / Daily-Digest überschreibt lokale YAML-Dateien mit altem Git-Stand
        -> Cloud-DB gewinnt beim Laden und stellt alle Benutzeränderungen zuverlässig wieder her!
        """
        old_sources_yaml = self.sources_file.read_text(encoding="utf-8")
        old_settings_yaml = self.settings_file.read_text(encoding="utf-8")

        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            # 1. Benutzer lädt Konfiguration, löscht 2 Feeds, fügt 1 neuen hinzu und ändert Retention-Tage
            cfg = load_sources(auto_reconcile=False)
            tech_cat = cfg["categories"][0]
            tech_cat["feeds"] = [
                f for f in tech_cat["feeds"]
                if f["url"] not in ("https://example.com/a.xml", "https://example.com/b.xml")
            ]
            tech_cat["feeds"].append({"name": "Feed Neu", "url": "https://example.com/new.xml"})
            cfg["settings"]["archive_retention_days"] = 21

            # 2. Speichern ohne GitHub-Push (simuliert fehlendes/ungültiges GITHUB_TOKEN)
            res = save_sources(cfg, sync_github=False, persist_db=True)
            self.assertTrue(res["db_saved"])

            # 3. Simuliere Streamlit-Cloud-Redeploy / GitHub-Action-Checkout nach KI-Briefing:
            #    Lokale Dateien werden komplett auf den ALTEN Git-Stand zurückgesetzt!
            self.sources_file.write_text(old_sources_yaml, encoding="utf-8")
            self.settings_file.write_text(old_settings_yaml, encoding="utf-8")
            invalidate_config_sync_cache()

            # 4. Nach dem KI-Briefing / Neustart: Konfiguration wird geladen (oder main.py ruft sync auf)
            statuses = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(statuses.get("sources.yaml"), "restored_from_db")
            self.assertEqual(statuses.get("settings.yaml"), "restored_from_db")

            restored_cfg = load_sources(auto_reconcile=False)
            feed_urls = {f["url"] for f in restored_cfg["categories"][0]["feeds"]}
            self.assertNotIn("https://example.com/a.xml", feed_urls)
            self.assertNotIn("https://example.com/b.xml", feed_urls)
            self.assertIn("https://example.com/c.xml", feed_urls)
            self.assertIn("https://example.com/new.xml", feed_urls)
            self.assertEqual(restored_cfg["settings"]["archive_retention_days"], 21)

    def test_consecutive_unsynced_ui_edits_preserve_original_git_base_hash(self) -> None:
        """Mehrere aufeinanderfolgende UI-Saves ohne GitHub-Push behalten den ursprünglichen Git-Basis-Hash."""
        initial_yaml = self.sources_file.read_text(encoding="utf-8")
        initial_hash = _sha256_text(initial_yaml)

        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            cfg1 = load_sources(auto_reconcile=False)
            cfg1["categories"][0]["feeds"].append({"name": "Edit 1", "url": "https://example.com/e1.xml"})
            save_sources(cfg1, sync_github=False, persist_db=True)

            cfg2 = load_sources(auto_reconcile=False)
            cfg2["categories"][0]["feeds"].append({"name": "Edit 2", "url": "https://example.com/e2.xml"})
            save_sources(cfg2, sync_github=False, persist_db=True)

            raw_snap = self.storage.get_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml")
            self.assertIsNotNone(raw_snap)
            snap = ConfigSnapshot.from_json(raw_snap or "")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.base_sha256, initial_hash)
            self.assertNotEqual(snap.sha256, initial_hash)

            # Redeploy mit initial_yaml -> Edit 2 muss vollständig wiederhergestellt werden
            self.sources_file.write_text(initial_yaml, encoding="utf-8")
            invalidate_config_sync_cache()
            reloaded = load_sources(auto_reconcile=False)
            urls = {f["url"] for f in reloaded["categories"][0]["feeds"]}
            self.assertIn("https://example.com/e1.xml", urls)
            self.assertIn("https://example.com/e2.xml", urls)

    def test_direct_git_commit_wins_when_db_was_in_sync(self) -> None:
        """Wenn DB und Git synchron waren (`sha256 == base_sha256`), wird ein direkter Git-Commit sauber übernommen."""
        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            cfg = load_sources(auto_reconcile=False)
            cfg["settings"]["archive_retention_days"] = 14
            save_sources(cfg, sync_github=False, persist_db=True)

            # Simuliere erfolgreichen GitHub-Sync -> base_sha256 == sha256
            _mark_configs_synced_to_git(
                [
                    ("config/sources.yaml", self.sources_file),
                    ("config/settings.yaml", self.settings_file),
                    ("config/prompts.yaml", self.prompts_file),
                ],
                storage=self.storage,
            )

            # Entwickler ändert settings.yaml direkt per git commit (auf 30 Tage)
            git_updated = dict(self.initial_settings)
            git_updated["archive_retention_days"] = 30
            self.settings_file.write_text(yaml.dump(git_updated, allow_unicode=True, sort_keys=False), encoding="utf-8")
            invalidate_config_sync_cache()

            statuses = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(statuses.get("settings.yaml"), "updated_db_from_disk")

            loaded = load_sources(auto_reconcile=False)
            self.assertEqual(loaded["settings"]["archive_retention_days"], 30)

    def test_corrupt_db_snapshot_fails_secure_and_keeps_disk_config(self) -> None:
        """Korruptes JSON oder YAML in der DB darf lokale Konfigurationsdateien nicht beschädigen."""
        self.storage.set_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml", "{not-valid-json")
        self.storage.set_metadata(
            f"{CONFIG_META_KEY_PREFIX}settings.yaml",
            json.dumps({"yaml": "- just a list, not a dict", "sha256": "abc", "base_sha256": "xyz", "updated_at": 1.0}),
        )
        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            statuses = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(statuses.get("sources.yaml"), "invalid_snapshot")
            self.assertEqual(statuses.get("settings.yaml"), "invalid_yaml")
            loaded = load_sources(auto_reconcile=False)
            self.assertEqual(len(loaded["categories"][0]["feeds"]), 3)
            self.assertEqual(loaded["settings"]["archive_retention_days"], 8)

    def test_exact_2302_cet_30s_ttl_poisoning_and_0601_utc_daily_digest_regression(self) -> None:
        """
        EXAKTER REGRESSIONSTEST für den gemeldeten Produktivfehler (24.11. 23:02 CET -> 25.11. 06:01 UTC):
        1. User löscht 2 Feeds, fügt 1 neuen hinzu und speichert -> GitHub liefert HTTP 401 (kein Push).
        2. >30s später (TTL abgelaufen): Streamlit Cloud rendert erneut und liest seine EIGENE lokale Disk (H2 == H2).
           -> `synced_to_git` DARF NICHT auf True gesetzt werden!
        3. Am nächsten Morgen (06:01 UTC) checkt `Daily News Digest` den alten Git-Stand (H1) aus.
           -> `sync_configs_from_storage(force=True)` MUSS H2 aus der DB wiederherstellen (H1 darf DB NIEMALS überschreiben)!
        4. Erst nach echtem `git push origin main` setzt `mark_canonical_configs_synced_to_git()` `synced_to_git=True`.
        5. Selbst ein veralteter Streamlit-Container (ohne .git oder älterem Commit) mit H1 darf danach H2 nicht zurückrollen.
        """
        old_sources_yaml = self.sources_file.read_text(encoding="utf-8")
        old_hash = _sha256_text(old_sources_yaml)

        mock_401 = MagicMock()
        mock_401.status_code = 401
        mock_401.history = []

        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
            patch(
                "src.sources_manager.get_github_sync_config",
                return_value={"token": "ghp_expired_token", "repo": "hschenke/news-bot", "branch": "main"},
            ),
            patch("src.sources_manager.requests.get", return_value=mock_401),
            patch("src.sources_manager.requests.put") as mock_put,
        ):
            # 1. Benutzer ändert Feeds und speichert (GitHub antwortet HTTP 401)
            cfg = load_sources(auto_reconcile=False)
            cfg["categories"][0]["feeds"] = [
                f for f in cfg["categories"][0]["feeds"]
                if f["url"] not in ("https://example.com/a.xml", "https://example.com/b.xml")
            ]
            cfg["categories"][0]["feeds"].append({"name": "Feed Neu", "url": "https://example.com/new.xml"})

            save_res = save_sources(cfg, sync_github=True, persist_db=True)
            self.assertTrue(save_res["db_saved"])
            self.assertTrue(save_res["db_verified"])
            self.assertFalse(save_res["success"])
            self.assertEqual(save_res["status_code"], 401)
            mock_put.assert_not_called()

            new_sources_yaml = self.sources_file.read_text(encoding="utf-8")
            new_hash = _sha256_text(new_sources_yaml)
            self.assertNotEqual(old_hash, new_hash)

            # 2. >30s TTL auf Streamlit Cloud abgelaufen (mehrere Reruns auf derselben lokalen Disk H2 == H2)
            for _ in range(3):
                invalidate_config_sync_cache()
                st_rerun = sync_configs_from_storage(force=False, storage=self.storage)
                self.assertEqual(st_rerun.get("sources.yaml"), "pending_git_sync")

            raw_after_reruns = self.storage.get_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml") or ""
            snap_after_reruns = ConfigSnapshot.from_json(raw_after_reruns)
            assert snap_after_reruns is not None
            self.assertFalse(snap_after_reruns.synced_to_git)
            self.assertEqual(snap_after_reruns.sha256, new_hash)
            self.assertEqual(snap_after_reruns.base_sha256, old_hash)

            # 3. 06:01 UTC Daily News Digest checkt alten Git-Stand H1 aus
            self.sources_file.write_text(old_sources_yaml, encoding="utf-8")
            invalidate_config_sync_cache()
            st_daily = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(st_daily.get("sources.yaml"), "restored_from_db")

            restored_cfg = load_sources(auto_reconcile=False)
            restored_urls = {f["url"] for f in restored_cfg["categories"][0]["feeds"]}
            self.assertEqual(restored_urls, {"https://example.com/c.xml", "https://example.com/new.xml"})

            # 4. Nach erfolgreichem `git push origin main`: CAS markiert H2 als synced_to_git=True
            cas_res = mark_canonical_configs_synced_to_git(storage=self.storage)
            self.assertEqual(cas_res.get("sources.yaml"), "marked_synced")
            snap_synced = ConfigSnapshot.from_json(self.storage.get_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml") or "")
            assert snap_synced is not None
            self.assertTrue(snap_synced.synced_to_git)
            self.assertEqual(snap_synced.git_sha256, new_hash)
            self.assertEqual(snap_synced.base_sha256, old_hash)

            # 5. Veralteter Container mit altem H1 darf H2 auch nach synced_to_git=True nicht überschreiben!
            self.sources_file.write_text(old_sources_yaml, encoding="utf-8")
            invalidate_config_sync_cache()
            st_stale = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(st_stale.get("sources.yaml"), "restored_from_db")
            self.assertEqual(_sha256_text(self.sources_file.read_text(encoding="utf-8")), new_hash)

    def test_cas_mid_workflow_race_preserves_newer_ui_edit(self) -> None:
        """
        Race-Condition-Schutz (CAS):
        Während Daily Digest Stand H2 verarbeitet und zu GitHub pusht, speichert der Benutzer in der UI bereits H3.
        `mark_canonical_configs_synced_to_git()` darf H3 NICHT mit H2 überschreiben!
        """
        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            # H2 speichern (Stand den Daily Digest geladen hat)
            cfg_h2 = load_sources(auto_reconcile=False)
            cfg_h2["categories"][0]["feeds"].append({"name": "Feed H2", "url": "https://example.com/h2.xml"})
            save_sources(cfg_h2, sync_github=False, persist_db=True)
            disk_h2_yaml = self.sources_file.read_text(encoding="utf-8")
            h2_hash = _sha256_text(disk_h2_yaml)

            # Währenddessen speichert User H3 in der Cloud-DB
            cfg_h3 = copy.deepcopy(cfg_h2)
            cfg_h3["categories"][0]["feeds"].append({"name": "Feed H3", "url": "https://example.com/h3.xml"})
            save_sources(cfg_h3, sync_github=False, persist_db=True)
            h3_hash = _sha256_text(self.sources_file.read_text(encoding="utf-8"))

            # Workflow-Runner hat auf seiner Disk noch H2 und ruft nach git push CAS-Bestätigung auf
            self.sources_file.write_text(disk_h2_yaml, encoding="utf-8")
            cas_res = mark_canonical_configs_synced_to_git(storage=self.storage)
            self.assertEqual(cas_res.get("sources.yaml"), "newer_db_preserved")

            snap = ConfigSnapshot.from_json(self.storage.get_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml") or "")
            assert snap is not None
            self.assertEqual(snap.sha256, h3_hash)
            self.assertEqual(snap.git_sha256, h2_hash)
            self.assertFalse(snap.synced_to_git)

            # Nächster Sync stellt H3 zuverlässig auf Disk wieder her
            invalidate_config_sync_cache()
            st_next = sync_configs_from_storage(force=True, storage=self.storage)
            self.assertEqual(st_next.get("sources.yaml"), "restored_from_db")
            urls = {f["url"] for f in load_sources(auto_reconcile=False)["categories"][0]["feeds"]}
            self.assertIn("https://example.com/h3.xml", urls)

    def test_turso_transient_fallback_auto_recovers_and_readback_verifies(self) -> None:
        """
        Prüft:
        1. Temporärer Turso-Fehler beim Start -> Fallback auf SqliteStorage (`is_cloud_storage=False`).
        2. `get_storage(force_reconnect=True)` wechselt nach Turso-Recovery sofort zurück zu TursoStorage!
        3. `_persist_configs_to_storage` unterscheidet ehrlich `is_cloud_db` (SQLite=False vs Turso=True)
           und schlägt bei fehlgeschlagener Read-Back-Verifikation sauber fehl.
        """
        reset_storage_singleton()
        try:
            with (
                patch("src.storage.get_turso_config", return_value=("libsql://mock.turso.io", "mock-key")),
                patch("src.storage.TursoStorage.init_db", side_effect=RuntimeError("transient network error")),
            ):
                fallback_db = get_storage(prefer_turso=True)
                self.assertIsInstance(fallback_db, SqliteStorage)
                self.assertFalse(is_cloud_storage(fallback_db))

            # Recovery über force_reconnect=True (wie beim expliziten Speichern)
            with (
                patch("src.storage.get_turso_config", return_value=("libsql://mock.turso.io", "mock-key")),
                patch("src.storage.TursoStorage.init_db", return_value=None),
            ):
                recovered_db = get_storage(prefer_turso=True, force_reconnect=True)
                self.assertIsInstance(recovered_db, TursoStorage)
                self.assertTrue(is_cloud_storage(recovered_db))
        finally:
            reset_storage_singleton()

        # Ehrliches Backend-Reporting bei SQLite vs. Turso + Read-Back-Verifikation
        yaml_payload = "categories: []\n"
        sqlite_res = _persist_configs_to_storage({"sources.yaml": ("old", yaml_payload)}, storage=self.storage)
        self.assertTrue(sqlite_res["db_saved"])
        self.assertTrue(sqlite_res["db_verified"])
        self.assertFalse(sqlite_res["is_cloud_db"])
        self.assertEqual(sqlite_res["storage_backend"], "sqlite")

        # Mocked TursoStorage mit echter Read-Back-Speicherung
        turso_mem: dict[str, str] = {}
        fake_turso = TursoStorage("libsql://mock.turso.io", "mock-token")
        with (
            patch.object(fake_turso, "get_metadata_many", side_effect=lambda keys: {k: turso_mem[k] for k in keys if k in turso_mem}),
            patch.object(fake_turso, "set_metadata_many", side_effect=lambda items: turso_mem.update(items)),
        ):
            turso_res = _persist_configs_to_storage({"sources.yaml": ("old", yaml_payload)}, storage=fake_turso)
        self.assertTrue(turso_res["db_saved"])
        self.assertTrue(turso_res["db_verified"])
        self.assertTrue(turso_res["is_cloud_db"])
        self.assertEqual(turso_res["storage_backend"], "turso")

        # Fehlgeschlagenes Read-Back (DB liefert nach Schreiben leeren Stand) -> db_saved=False, db_verified=False
        with (
            patch.object(fake_turso, "get_metadata_many", return_value={}),
            patch.object(fake_turso, "set_metadata_many", return_value=None),
        ):
            bad_res = _persist_configs_to_storage({"sources.yaml": ("old", yaml_payload)}, storage=fake_turso)
        self.assertFalse(bad_res["db_saved"])
        self.assertFalse(bad_res["db_verified"])
        self.assertIn("Read-Back", bad_res["db_error"] or "")


class TestHardenedGitHubSync(unittest.TestCase):
    """Prüft Redirect-Auflösung (Repo-Rename), 422-Konflikt-Retry, 401/403 Short-Circuit und Token-Sanitization."""

    def test_sanitize_github_token_strips_quotes_and_auth_prefixes(self) -> None:
        self.assertEqual(_sanitize_github_token('  "ghp_abc123"  '), "ghp_abc123")
        self.assertEqual(_sanitize_github_token("'github_pat_xyz'"), "github_pat_xyz")
        self.assertEqual(_sanitize_github_token("Bearer ghp_bearer_token"), "ghp_bearer_token")
        self.assertEqual(_sanitize_github_token("token ghp_classic_token"), "ghp_classic_token")
        self.assertEqual(_sanitize_github_token(None), "")

    def test_http_401_and_403_short_circuit_without_contents_api_fallback(self) -> None:
        """Bei HTTP 401/403 darf sync_sources_to_github KEINE Contents-API-Fallbacks (6x 401) mehr ausführen."""
        for status_code in (401, 403):
            resp = MagicMock()
            resp.status_code = status_code
            resp.history = []

            with (
                patch(
                    "src.sources_manager.get_github_sync_config",
                    return_value={"token": "ghp_secret_must_not_leak", "repo": "hschenke/news-bot", "branch": "main"},
                ),
                patch("src.sources_manager.requests.get", return_value=resp) as mock_get,
                patch("src.sources_manager.requests.put") as mock_put,
                patch("src.sources_manager._mark_configs_synced_to_git") as mock_mark,
            ):
                result = sync_sources_to_github(config_dict=None, include_rss_feeds=False)

            self.assertFalse(result["success"])
            self.assertEqual(result["status_code"], status_code)
            self.assertIn(f"HTTP {status_code}", result["error"])
            self.assertNotIn("ghp_secret_must_not_leak", result["error"])
            self.assertEqual(mock_get.call_count, 1)
            mock_put.assert_not_called()
            mock_mark.assert_not_called()

    def test_sync_resolves_renamed_repo_and_retries_422_conflict(self) -> None:
        """Simuliert altes Repo in Secrets (`news-aggregator-bot`) + HTTP 422 beim ersten Ref-Update."""
        get_calls: list[str] = []
        patch_urls: list[str] = []
        patch_attempt = {"count": 0}

        def fake_get(url: str, headers: dict, timeout: int = 8) -> MagicMock:
            get_calls.append(url)
            resp = MagicMock()
            if url == "https://api.github.com/repos/hschenke/news-aggregator-bot/git/ref/heads/main":
                resp.status_code = 200
                resp.history = [MagicMock(status_code=301)]
                resp.json.return_value = {"object": {"sha": "old_sha"}}
                return resp
            if url == "https://api.github.com/repos/hschenke/news-aggregator-bot":
                resp.status_code = 200
                resp.history = []
                resp.json.return_value = {"full_name": "hschenke/news-bot"}
                return resp
            if url == "https://api.github.com/repos/hschenke/news-bot/git/ref/heads/main":
                resp.status_code = 200
                resp.history = []
                sha = "head_sha_v2" if patch_attempt["count"] > 0 else "head_sha_v1"
                resp.json.return_value = {"object": {"sha": sha}}
                return resp
            if "/git/commits/" in url:
                resp.status_code = 200
                resp.history = []
                resp.json.return_value = {"tree": {"sha": "base_tree_sha"}}
                return resp
            resp.status_code = 404
            resp.history = []
            return resp

        def fake_post(url: str, headers: dict, json: dict, timeout: int = 12) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 201
            if url.endswith("/git/trees"):
                # Sicherstellen, dass bei include_rss_feeds=False keine XML-Dateien enthalten sind
                paths = [el["path"] for el in json["tree"]]
                self.assertTrue(all(p.startswith("config/") for p in paths))
                resp.json.return_value = {"sha": "new_tree_sha"}
            else:
                resp.json.return_value = {"sha": "new_commit_sha"}
            return resp

        def fake_patch(url: str, headers: dict, json: dict, timeout: int = 10) -> MagicMock:
            patch_urls.append(url)
            patch_attempt["count"] += 1
            resp = MagicMock()
            # Erster Versuch -> 422 Fast-Forward-Konflikt; zweiter Versuch -> 200 OK
            resp.status_code = 422 if patch_attempt["count"] == 1 else 200
            return resp

        with (
            patch(
                "src.sources_manager.get_github_sync_config",
                return_value={"token": "ghp_valid_test_token", "repo": "hschenke/news-aggregator-bot", "branch": "main"},
            ),
            patch("src.sources_manager.requests.get", side_effect=fake_get),
            patch("src.sources_manager.requests.post", side_effect=fake_post),
            patch("src.sources_manager.requests.patch", side_effect=fake_patch),
            patch("src.sources_manager._mark_configs_synced_to_git") as mock_mark,
            patch("src.sources_manager.purge_jsdelivr_cache") as mock_purge,
        ):
            result = sync_sources_to_github(
                config_dict=None,
                commit_message="chore(config): test",
                include_rss_feeds=False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["repo"], "hschenke/news-bot")
        self.assertEqual(result["commit_sha"], "new_commit_sha")
        self.assertEqual(len(patch_urls), 2)
        self.assertTrue(all("hschenke/news-bot" in u for u in patch_urls))
        mock_mark.assert_called_once()
        mock_purge.assert_not_called()


class TestBatchMetadataAndDeletedFeedCleanup(unittest.TestCase):
    """Prüft Batch-Metadaten (SQLite & Turso) sowie das Bereinigen gelöschter Feed-Artikel."""

    def test_sqlite_batch_metadata_and_delete_by_source_url(self) -> None:
        storage = SqliteStorage(":memory:")
        storage.init_db()
        try:
            storage.set_metadata_many({"k1": "v1", "k2": "v2"})
            loaded = storage.get_metadata_many(["k1", "k2", "k3"])
            self.assertEqual(loaded, {"k1": "v1", "k2": "v2"})

            storage.save_articles([
                Article(title="Keep", link="https://ex.com/keep", source="KeepFeed", source_url="https://ex.com/keep.xml", category="Tech"),
                Article(title="Drop 1", link="https://ex.com/drop1", source="DropFeed", source_url="https://ex.com/drop.xml", category="Tech"),
                Article(title="Drop 2", link="https://ex.com/drop2", source="DropFeed", source_url="https://ex.com/drop.xml", category="Tech"),
            ])
            deleted = storage.delete_articles_by_source_urls(["https://ex.com/drop.xml"])
            self.assertEqual(deleted, 2)
            remaining = storage.get_articles()
            self.assertEqual([a.link for a in remaining], ["https://ex.com/keep"])
        finally:
            storage.close()

    @patch("requests.Session.post")
    def test_turso_batch_metadata_uses_single_pipeline_call(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "type": "ok",
                    "response": {
                        "type": "execute",
                        "result": {
                            "cols": [{"name": "key"}, {"name": "value"}],
                            "rows": [
                                [{"type": "text", "value": "config_store:sources.yaml"}, {"type": "text", "value": "{}"}],
                            ],
                            "affected_row_count": 0,
                            "last_insert_rowid": None,
                        },
                    },
                }
            ]
        }
        mock_post.return_value = mock_resp

        turso = TursoStorage("libsql://mock.turso.io", "mock-token")
        got = turso.get_metadata_many(["config_store:sources.yaml", "config_store:settings.yaml"])
        self.assertEqual(got, {"config_store:sources.yaml": "{}"})
        self.assertEqual(mock_post.call_count, 1)

        mock_post.reset_mock()
        turso.set_metadata_many({"a": "1", "b": "2", "c": "3"})
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args[1]["json"]
        # 3 execute statements + 1 close statement in einer einzigen HTTP-Pipeline
        self.assertEqual(len(payload["requests"]), 4)


class TestSsrfFeedUrlValidation(unittest.TestCase):
    """Prüft OWASP A10 SSRF-Schutz in validate_feed_url."""

    def test_allows_public_http_and_https_urls(self) -> None:
        for url in (
            "https://www.heise.de/rss/heise-atom.xml",
            "http://example.com/feed.xml",
        ):
            ok, err = validate_feed_url(url, resolve_dns=False)
            self.assertTrue(ok, f"Public URL sollte erlaubt sein: {url} ({err})")

    def test_blocks_forbidden_schemes_and_private_targets(self) -> None:
        forbidden_urls = [
            "file:///etc/passwd",
            "ftp://example.com/feed.xml",
            "gopher://127.0.0.1:6379/_INFO",
            "http://localhost/feed.xml",
            "http://127.0.0.1:8080/feed.xml",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal.xml",
            "http://172.16.0.1/feed.xml",
            "http://192.168.1.1/feed.xml",
            "http://[::1]/feed.xml",
            "http://metadata.google.internal/computeMetadata/v1/",
        ]
        for url in forbidden_urls:
            ok, err = validate_feed_url(url, resolve_dns=False)
            self.assertFalse(ok, f"SSRF-URL hätte blockiert werden müssen: {url}")
            self.assertIsNotNone(err)

    def test_blocks_dns_rebinding_to_private_ip(self) -> None:
        fake_addr = [(2, 1, 6, "", ("127.0.0.1", 0))]
        with patch("src.feed_fetcher.socket.getaddrinfo", return_value=fake_addr):
            ok, err = validate_feed_url("https://rebind.attacker.example/feed.xml", resolve_dns=True)
        self.assertFalse(ok)
        self.assertIn("privaten oder lokalen", err or "")


class TestManageTabAndStateIntegration(unittest.TestCase):
    """Prüft Manage-Tab-Hilfslogik, Cache-Filterung für gelöschte Feeds und Workflow-Integrität."""

    def test_get_news_data_filters_out_deleted_feed_source_urls(self) -> None:
        """Artikel aus gelöschten Feeds (mit source_url) dürfen nicht aus der DB in Artikel/KI-Tab zurückkehren."""
        import time
        import streamlit as st
        from src.ui.state import get_news_data

        now = time.time()
        articles = [
            Article(
                title="Aktiver Feed Artikel",
                link="https://ex.com/active-1",
                category="Tech & AI",
                source="Heise Online News",
                source_url="https://www.heise.de/rss/heise-atom.xml",
                timestamp=now,
            ),
            Article(
                title="Gelöschter Feed Artikel",
                link="https://ex.com/deleted-1",
                category="Tech & AI",
                source="Alter Feed",
                source_url="https://deleted-feed.example.com/rss.xml",
                timestamp=now,
            ),
        ]

        with (
            patch("src.ui.state.get_storage") as mock_get_storage,
            patch("src.ui.state.collect_all_news") as mock_collect,
        ):
            mock_storage = MagicMock()
            mock_storage.get_articles.return_value = articles
            mock_get_storage.return_value = mock_storage

            get_news_data.clear()
            st.cache_data.clear()
            data = get_news_data(force_live_fetch=False)

        self.assertIn("Tech & AI", data)
        titles = [a["title"] for a in data["Tech & AI"]]
        self.assertIn("Aktiver Feed Artikel", titles)
        self.assertNotIn("Gelöschter Feed Artikel", titles)
        mock_collect.assert_not_called()

    def test_manage_tab_save_prunes_deleted_feeds_and_preserves_prompts(self) -> None:
        """Beim Speichern im Verwalten-Tab werden gelöschte Feeds aus dem Cache entfernt und KI-Prompts erhalten."""
        from src.ui.tabs.manage_tab import (
            _build_save_notice,
            _preserve_latest_prompts,
            _sync_news_cache_after_save,
        )
        import streamlit as st

        # 1. Prompts aus prompts.yaml werden beim Verwalten-Speichern nicht überschrieben
        working = {"categories": [], "settings": {"language": "de", "custom_main_prompt": "Alt"}}
        with patch("src.ui.tabs.manage_tab.load_prompts", return_value={"custom_main_prompt": "Neu aus KI-Tab"}):
            _preserve_latest_prompts(working)
        self.assertEqual(working["settings"]["custom_main_prompt"], "Neu aus KI-Tab")

        # 2. Strikt ehrliche Notice-Formatierung: Grün NUR bei verifizierter Turso Cloud-DB + GitHub OK!
        lvl1, icon1, msg1 = _build_save_notice(
            {"db_saved": True, "db_verified": True, "is_cloud_db": True, "storage_backend": "turso", "success": True},
            newly_fetched_count=3,
        )
        self.assertEqual((lvl1, icon1), ("success", "✅"))
        self.assertIn("Cloud-Datenbank & GitHub", msg1)
        self.assertIn("3 neue Artikel", msg1)

        # Turso Cloud-DB OK, aber GitHub HTTP 401 -> WARNUNG (niemals grünes OK!)
        lvl2, icon2, msg2 = _build_save_notice(
            {
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": True,
                "storage_backend": "turso",
                "success": False,
                "status_code": 401,
                "error": "GitHub-Token ungültig oder abgelaufen (HTTP 401)",
            }
        )
        self.assertEqual((lvl2, icon2), ("warning", "⚠️"))
        self.assertIn("Turso Cloud-Datenbank", msg2)
        self.assertIn("HTTP 401", msg2)

        # Nur lokale SQLite + GitHub HTTP 401 -> FEHLER (rot, nicht dauerhaft gespeichert!)
        lvl3, icon3, msg3 = _build_save_notice(
            {
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": False,
                "storage_backend": "sqlite",
                "success": False,
                "status_code": 401,
                "error": "HTTP 401",
            }
        )
        self.assertEqual((lvl3, icon3), ("error", "❌"))
        self.assertIn("NICHT dauerhaft gespeichert", msg3)
        self.assertIn("SQLite", msg3)

        # Read-Back-Verifikation fehlgeschlagen + GitHub OK -> WARNUNG (kein grünes OK)
        lvl4, icon4, msg4 = _build_save_notice(
            {
                "db_saved": False,
                "db_verified": False,
                "is_cloud_db": True,
                "storage_backend": "turso",
                "db_error": "Read-Back fehlgeschlagen",
                "success": True,
            }
        )
        self.assertEqual((lvl4, icon4), ("warning", "⚠️"))
        self.assertIn("Read-Back", msg4)

        # Auch KI-Tab-Prompt-Speicherung muss strikt ehrlich warnen/fehlen
        from src.ui.tabs.ki_tab import _build_prompt_save_notice

        k_lvl_ok, k_icon_ok, _ = _build_prompt_save_notice(
            {"db_saved": True, "db_verified": True, "is_cloud_db": True, "success": True}
        )
        self.assertEqual((k_lvl_ok, k_icon_ok), ("success", "✅"))

        k_lvl_warn, k_icon_warn, k_msg_warn = _build_prompt_save_notice(
            {"db_saved": True, "db_verified": True, "is_cloud_db": True, "success": False, "error": "HTTP 401"}
        )
        self.assertEqual((k_lvl_warn, k_icon_warn), ("warning", "⚠️"))
        self.assertIn("HTTP 401", k_msg_warn)

        k_lvl_err, k_icon_err, _ = _build_prompt_save_notice(
            {"db_saved": True, "db_verified": True, "is_cloud_db": False, "storage_backend": "sqlite", "success": False, "error": "HTTP 401"}
        )
        self.assertEqual((k_lvl_err, k_icon_err), ("error", "❌"))

        # 3. _sync_news_cache_after_save entfernt gelöschte Feeds & lädt neue nach
        prev_cfg = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [
                        {"name": "Keep", "url": "https://example.com/keep.xml"},
                        {"name": "Del1", "url": "https://example.com/del1.xml"},
                        {"name": "Del2", "url": "https://example.com/del2.xml"},
                    ],
                }
            ],
            "settings": {"filter_ads": True, "ad_patterns": []},
        }
        new_cfg = copy.deepcopy(prev_cfg)
        new_cfg["categories"][0]["feeds"] = [
            {"name": "Keep", "url": "https://example.com/keep.xml"},
            {"name": "Added", "url": "https://example.com/added.xml"},
        ]

        st.session_state["cached_news_data"] = {
            "Tech & AI": [
                {"title": "Keep Art", "link": "https://ex.com/k", "source": "Keep", "source_url": "https://example.com/keep.xml", "timestamp": 100.0},
                {"title": "Del1 Art", "link": "https://ex.com/d1", "source": "Del1", "source_url": "https://example.com/del1.xml", "timestamp": 90.0},
                {"title": "Del2 Art", "link": "https://ex.com/d2", "source": "Del2", "source_url": "https://example.com/del2.xml", "timestamp": 80.0},
            ]
        }

        with (
            patch("src.ui.tabs.manage_tab.run_in_background") as mock_bg,
            patch(
                "src.ui.tabs.manage_tab.fetch_feed_items",
                return_value=[{"title": "Fresh Added", "link": "https://ex.com/new1", "timestamp": 200.0}],
            ),
        ):
            fetched_cnt = _sync_news_cache_after_save(prev_cfg, new_cfg)

        self.assertEqual(fetched_cnt, 1)
        remaining_titles = [a["title"] for a in st.session_state["cached_news_data"]["Tech & AI"]]
        self.assertIn("Keep Art", remaining_titles)
        self.assertIn("Fresh Added", remaining_titles)
        self.assertNotIn("Del1 Art", remaining_titles)
        self.assertNotIn("Del2 Art", remaining_titles)
        self.assertGreaterEqual(mock_bg.call_count, 1)

    def test_workflows_sync_and_commit_cloud_db_configs(self) -> None:
        """Daily Digest & Update RSS Workflows müssen Cloud-DB-Konfigurationen synchronisieren, pushen und via CAS bestätigen."""
        root = Path(__file__).resolve().parent.parent
        daily = (root / ".github" / "workflows" / "daily_digest.yaml").read_text(encoding="utf-8")
        update_rss = (root / ".github" / "workflows" / "update_rss.yaml").read_text(encoding="utf-8")
        main_py = (root / "src" / "main.py").read_text(encoding="utf-8")

        self.assertIn("config/sources.yaml config/settings.yaml config/prompts.yaml", daily)
        self.assertIn("TURSO_URL: ${{ secrets.TURSO_URL }}", update_rss)
        self.assertIn("TURSO_KEY: ${{ secrets.TURSO_KEY }}", update_rss)
        self.assertIn("sync_configs_from_storage(force=True)", update_rss)
        self.assertIn("config/sources.yaml config/settings.yaml config/prompts.yaml", update_rss)
        self.assertIn("sync_configs_from_storage(force=True)", main_py)

        for wf in (daily, update_rss):
            self.assertIn("group: news-bot-main-sync", wf)
            self.assertIn("cancel-in-progress: false", wf)
            self.assertIn("git rebase -X theirs origin/main", wf)
            self.assertIn("mark_canonical_configs_synced_to_git()", wf)
            # CAS-Bestätigung MUSS nach `git push origin main` erfolgen
            self.assertGreater(
                wf.find("mark_canonical_configs_synced_to_git()"),
                wf.find("git push origin main"),
            )

    def test_manage_tab_e2e_delete_add_and_pending_settings_save_flow(self) -> None:
        """End-to-End Streamlit AppTest: Feed löschen, neuen Feed anlegen, globale Einstellung ändern & direkt sichern."""
        from streamlit.testing.v1 import AppTest

        def _run_manage() -> None:
            import streamlit as st
            from src.ui.tabs.manage_tab import render_manage_tab

            render_manage_tab(st.session_state["working_sources_config"], is_admin=True)

        initial_cfg = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [
                        {"name": "Feed Alpha", "url": "https://example.com/1.xml"},
                        {"name": "Feed Beta", "url": "https://example.com/2.xml"},
                        {"name": "Feed Gamma", "url": "https://example.com/3.xml"},
                    ],
                }
            ],
            "settings": {
                "language": "de",
                "archive_retention_days": 8,
                "summary_style": "tldr",
                "filter_ads": True,
                "ad_patterns": ["^anzeige:"],
            },
        }

        saved_capture: dict[str, object] = {}

        def fake_save_sources(cfg: dict, **kwargs: object) -> dict:
            saved_capture["config"] = copy.deepcopy(cfg)
            saved_capture["kwargs"] = kwargs
            return {
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": True,
                "storage_backend": "turso",
                "success": True,
                "commit_sha": "abc1234",
            }

        with (
            patch("src.ui.tabs.manage_tab.save_sources", side_effect=fake_save_sources),
            patch("src.ui.tabs.manage_tab.load_sources", side_effect=lambda: copy.deepcopy(saved_capture.get("config", initial_cfg))),
            patch("src.ui.tabs.manage_tab.load_prompts", return_value={"custom_main_prompt": "Keep Prompt"}),
            patch("src.ui.tabs.manage_tab.fetch_feed_items", return_value=[]),
            patch("src.ui.tabs.manage_tab.run_in_background"),
        ):
            at = AppTest.from_function(_run_manage, default_timeout=20)
            at.session_state["working_sources_config"] = copy.deepcopy(initial_cfg)
            at.session_state["last_loaded_saved_config"] = copy.deepcopy(initial_cfg)
            at.session_state["has_unsaved_changes"] = False
            at.run()
            self.assertFalse(at.exception, f"Initial render error: {at.exception}")

            # 1. Zwei Feeds löschen (direkt im Entwurf wie durch die UI-Buttons)
            del_btns = [b for b in at.button if (b.key or "").startswith("del_feed_")]
            self.assertEqual(len(del_btns), 3)
            del_btns[0].click().run()
            self.assertFalse(at.exception)
            del_btns_2 = [b for b in at.button if (b.key or "").startswith("del_feed_")]
            self.assertEqual(len(del_btns_2), 2)
            del_btns_2[0].click().run()
            self.assertFalse(at.exception)

            # 2. Neuen Feed hinzufügen
            next(ti for ti in at.text_input if ti.key == "inp_new_feed_url").set_value("https://example.com/fresh.xml")
            next(ti for ti in at.text_input if ti.key == "inp_new_feed_name").set_value("Neuer Feed")
            next(b for b in at.button if b.key == "btn_submit_new_feed").click().run()
            self.assertFalse(at.exception)

            # 3. Globale Einstellung direkt ändern (ohne erst 'Einstellungen im Entwurf übernehmen' zu klicken!)
            next(ni for ni in at.number_input if ni.key == "inp_set_retention_days").set_value(19)

            # 4. Auf '💾 Jetzt sichern' klicken
            save_btn = next(b for b in at.button if b.key == "top_save_sources_btn")
            self.assertFalse(save_btn.disabled)
            save_btn.click().run()
            self.assertFalse(at.exception, f"Save error: {at.exception}")

        saved_cfg = saved_capture["config"]
        assert isinstance(saved_cfg, dict)
        final_urls = {f["url"] for f in saved_cfg["categories"][0]["feeds"]}
        self.assertEqual(final_urls, {"https://example.com/3.xml", "https://example.com/fresh.xml"})
        self.assertEqual(saved_cfg["settings"]["archive_retention_days"], 19)
        self.assertEqual(saved_cfg["settings"]["custom_main_prompt"], "Keep Prompt")
        self.assertNotIn("streamlit_app_url", saved_cfg["settings"])
        self.assertEqual(saved_capture["kwargs"], {"sync_github": True, "persist_db": True, "include_rss_feeds": False})
        self.assertFalse(at.session_state["has_unsaved_changes"])
        self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "success")
        self.assertIn("Cloud-Datenbank & GitHub", str(at.session_state.get("manage_notice", "")))

    def test_manage_tab_e2e_honest_warning_error_and_inline_feed_edit(self) -> None:
        """
        End-to-End Streamlit AppTest für ehrliche Fehler-/Warnmeldungen & Inline-Feed-Edits:
        1. Inline-Feed-Name geändert + direkt '💾 Jetzt sichern' geklickt -> Änderung wird übernommen!
        2. Bei Turso OK + GitHub HTTP 401 -> Warn-Banner (`warning`, niemals `success`!).
        3. Bei lokalem SQLite + GitHub HTTP 401 -> Error-Banner (`error`) UND Save-Button bleibt für Retry aktiv!
        """
        from streamlit.testing.v1 import AppTest

        def _run_manage() -> None:
            import streamlit as st
            from src.ui.tabs.manage_tab import render_manage_tab

            render_manage_tab(st.session_state["working_sources_config"], is_admin=True)

        initial_cfg = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [{"name": "Alter Feed", "url": "https://example.com/feed.xml"}],
                }
            ],
            "settings": {"language": "de", "archive_retention_days": 8, "summary_style": "tldr", "filter_ads": True, "ad_patterns": []},
        }

        # Fall A: Inline-Edit + Turso OK + GitHub 401 -> Warning
        captured_a: dict[str, object] = {}

        def save_turso_401(cfg: dict, **kwargs: object) -> dict:
            captured_a["config"] = copy.deepcopy(cfg)
            return {
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": True,
                "storage_backend": "turso",
                "success": False,
                "status_code": 401,
                "error": "GitHub-Token ungültig oder abgelaufen (HTTP 401)",
            }

        with (
            patch("src.ui.tabs.manage_tab.save_sources", side_effect=save_turso_401),
            patch("src.ui.tabs.manage_tab.load_sources", side_effect=lambda: copy.deepcopy(captured_a.get("config", initial_cfg))),
            patch("src.ui.tabs.manage_tab.load_prompts", return_value={}),
            patch("src.ui.tabs.manage_tab.fetch_feed_items", return_value=[]),
            patch("src.ui.tabs.manage_tab.run_in_background"),
        ):
            at_a = AppTest.from_function(_run_manage, default_timeout=20)
            at_a.session_state["working_sources_config"] = copy.deepcopy(initial_cfg)
            at_a.session_state["last_loaded_saved_config"] = copy.deepcopy(initial_cfg)
            at_a.session_state["has_unsaved_changes"] = False
            at_a.run()

            name_input = next(ti for ti in at_a.text_input if (ti.key or "").startswith("ed_name_"))
            name_input.set_value("Umbenannter Feed").run()
            save_btn = next(b for b in at_a.button if b.key == "top_save_sources_btn")
            self.assertFalse(save_btn.disabled)
            save_btn.click().run()
            self.assertFalse(at_a.exception)

        saved_a = captured_a["config"]
        assert isinstance(saved_a, dict)
        self.assertEqual(saved_a["categories"][0]["feeds"][0]["name"], "Umbenannter Feed")
        self.assertEqual(at_a.session_state.get("_notice_level_manage_notice"), "warning")
        self.assertEqual(at_a.session_state.get("_notice_icon_manage_notice"), "⚠️")
        self.assertIn("HTTP 401", str(at_a.session_state.get("manage_notice", "")))
        self.assertFalse(at_a.session_state["has_unsaved_changes"])

        # Fall B: Nur lokales SQLite + GitHub 401 -> Error & Save-Button bleibt aktiv für Retry
        def save_sqlite_401(cfg: dict, **kwargs: object) -> dict:
            return {
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": False,
                "storage_backend": "sqlite",
                "success": False,
                "status_code": 401,
                "error": "GitHub-Token ungültig oder abgelaufen (HTTP 401)",
            }

        with (
            patch("src.ui.tabs.manage_tab.save_sources", side_effect=save_sqlite_401),
            patch("src.ui.tabs.manage_tab.load_sources", return_value=copy.deepcopy(initial_cfg)),
            patch("src.ui.tabs.manage_tab.load_prompts", return_value={}),
            patch("src.ui.tabs.manage_tab.fetch_feed_items", return_value=[]),
            patch("src.ui.tabs.manage_tab.run_in_background"),
        ):
            at_b = AppTest.from_function(_run_manage, default_timeout=20)
            at_b.session_state["working_sources_config"] = copy.deepcopy(initial_cfg)
            at_b.session_state["last_loaded_saved_config"] = copy.deepcopy(initial_cfg)
            at_b.session_state["has_unsaved_changes"] = True
            at_b.run()

            next(b for b in at_b.button if b.key == "top_save_sources_btn").click().run()
            self.assertFalse(at_b.exception)

        self.assertEqual(at_b.session_state.get("_notice_level_manage_notice"), "error")
        self.assertEqual(at_b.session_state.get("_notice_icon_manage_notice"), "❌")
        self.assertTrue(at_b.session_state["has_unsaved_changes"])
        self.assertIn("NICHT dauerhaft gespeichert", str(at_b.session_state.get("manage_notice", "")))

    def test_streamlit_app_url_only_from_environment_variable(self) -> None:
        """STREAMLIT_APP_URL darf ausschließlich aus der Umgebungsvariable gelesen werden, niemals statisch oder aus settings.yaml."""
        import os

        from src.summarizer import get_streamlit_app_url

        with patch.dict("os.environ", {"STREAMLIT_APP_URL": "https://holli-news-bot.streamlit.app/"}, clear=False):
            self.assertEqual(get_streamlit_app_url(), "https://holli-news-bot.streamlit.app")

        env_without_url = {k: v for k, v in os.environ.items() if k != "STREAMLIT_APP_URL"}
        with patch.dict("os.environ", env_without_url, clear=True):
            self.assertEqual(get_streamlit_app_url(), "")


if __name__ == "__main__":
    unittest.main()
