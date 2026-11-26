"""
Umfassende Tests für DB-only Konfigurationspersistenz (Turso/SQLite als Single Source of Truth),
strikte Fail-Closed-Verifikation & Rollback-Reload auf alte Werte, sofortiges Speichern ohne
Entwurfs-Sammlung, RSS-only GitHub-Sync, SSRF-Schutz und schlanke Workflow-Integrität.
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
    _persist_configs_to_storage,
    _sanitize_github_token,
    ensure_db_configs_seeded,
    invalidate_config_cache,
    load_prompts,
    load_settings,
    load_sources,
    save_prompts,
    save_settings,
    save_sources,
    sync_sources_to_github,
)
from src.storage import get_storage, is_cloud_storage, reset_storage_singleton
from src.storage.sqlite import SqliteStorage
from src.storage.turso import TursoStorage


class TestDbOnlyConfigPersistence(unittest.TestCase):
    """Prüft Datenbank-only Persistenz, einmaliges Seeding, Cache, Read-Back und Fail-Closed-Verhalten."""

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
            "summary_style": "tldr",
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
        invalidate_config_cache()

    def tearDown(self) -> None:
        invalidate_config_cache()
        self.storage.close()
        self.tmp_dir.cleanup()

    def test_db_is_single_source_of_truth_and_ignores_stale_repo_yaml(self) -> None:
        """
        1. Leere DB wird einmalig aus config/*.yaml geseeded.
        2. Nach Speichern in DB bleiben lokale YAML-Dateien unangetastet und können DB NIEMALS überschreiben
           (kein Git-Sync nötig, kein 401-Problem, kein Redeploy-Verlust!).
        """
        initial_yaml_on_disk = self.sources_file.read_text(encoding="utf-8")

        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
            patch("src.storage.get_turso_config", return_value=(None, None)),
        ):
            # 1. Erster Start auf leerer DB -> einmaliges Seeding
            statuses = ensure_db_configs_seeded(force=True, storage=self.storage)
            self.assertEqual(statuses["sources.yaml"], "seeded_to_db")
            self.assertEqual(statuses["settings.yaml"], "seeded_to_db")
            self.assertEqual(statuses["prompts.yaml"], "seeded_to_db")

            # 2. Benutzer löscht 2 Feeds, fügt 1 neuen hinzu und ändert Einstellung -> sofort in DB
            cfg = load_sources(auto_reconcile=False)
            cfg["categories"][0]["feeds"] = [
                f for f in cfg["categories"][0]["feeds"]
                if f["url"] not in ("https://example.com/a.xml", "https://example.com/b.xml")
            ]
            cfg["categories"][0]["feeds"].append({"name": "Feed Neu", "url": "https://example.com/new.xml"})
            cfg["settings"]["archive_retention_days"] = 21

            res = save_sources(cfg, sync_github=False, persist_db=True)
            self.assertTrue(res["success"])
            self.assertTrue(res["db_saved"])
            self.assertTrue(res["db_verified"])

            # Repo-YAML auf Disk wird nicht mehr angefasst (DB-only!)
            self.assertEqual(self.sources_file.read_text(encoding="utf-8"), initial_yaml_on_disk)

            # 3. Simuliere Container-Neustart / GitHub-Action-Checkout mit alten YAML-Dateien
            invalidate_config_cache()
            reloaded = load_sources(auto_reconcile=False)
            feed_urls = {f["url"] for f in reloaded["categories"][0]["feeds"]}
            self.assertEqual(feed_urls, {"https://example.com/c.xml", "https://example.com/new.xml"})
            self.assertEqual(reloaded["settings"]["archive_retention_days"], 21)

    def test_legacy_v045_turso_snapshots_load_seamlessly(self) -> None:
        """Bestehende Legacy-Snapshots (mit base_sha256/synced_to_git/git_sha256) werden 100 % verlustfrei gelesen."""
        legacy_payload = json.dumps(
            {
                "yaml": yaml.dump(
                    {"categories": [{"name": "LegacyCat", "feeds": [{"name": "L1", "url": "https://example.com/l1.xml"}]}]},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                "sha256": "ignored_or_valid",
                "base_sha256": "old_hash",
                "synced_to_git": False,
                "git_sha256": "old_hash",
                "updated_at": 1700000000.0,
            }
        )
        self.storage.set_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml", legacy_payload)

        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            loaded = load_sources(auto_reconcile=False)
            self.assertEqual(loaded["categories"][0]["name"], "LegacyCat")
            self.assertEqual(loaded["categories"][0]["feeds"][0]["url"], "https://example.com/l1.xml")

    def test_in_memory_cache_avoids_redundant_db_reads(self) -> None:
        """Innerhalb der 30s-TTL lösen wiederholte load_sources/load_settings/load_prompts genau 1 DB-Read aus."""
        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
            patch.object(self.storage, "get_metadata_many", wraps=self.storage.get_metadata_many) as spy_get,
        ):
            load_sources(auto_reconcile=False)
            load_settings()
            load_prompts()
            load_sources(auto_reconcile=False)
            self.assertEqual(spy_get.call_count, 1)

    def test_corrupt_db_snapshot_fails_secure_to_seed(self) -> None:
        """Korruptes JSON oder YAML in der DB bringt das Laden nicht zum Absturz."""
        self.storage.set_metadata(f"{CONFIG_META_KEY_PREFIX}sources.yaml", "{not-valid-json")
        self.storage.set_metadata(
            f"{CONFIG_META_KEY_PREFIX}settings.yaml",
            json.dumps({"yaml": "- just a list, not a dict", "sha256": "abc", "updated_at": 1.0}),
        )
        with (
            patch("src.sources_manager._get_project_root", return_value=self.root),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            statuses = ensure_db_configs_seeded(force=True, storage=self.storage)
            self.assertEqual(statuses["sources.yaml"], "invalid_db_fallback")
            self.assertEqual(statuses["settings.yaml"], "invalid_db_fallback")
            loaded = load_sources(auto_reconcile=False)
            self.assertEqual(len(loaded["categories"][0]["feeds"]), 3)
            self.assertEqual(loaded["settings"]["archive_retention_days"], 8)

    def test_fail_closed_rejects_sqlite_fallback_when_turso_configured(self) -> None:
        """
        Strikt Fail-Closed:
        Wenn TURSO_URL + TURSO_KEY gesetzt sind (Cloud-Betrieb), MUSS ein lokaler SQLite-Fallback
        beim Speichern als FEHLER (`success=False`) zurückgewiesen werden!
        """
        with (
            patch("src.storage.get_turso_config", return_value=("libsql://mock.turso.io", "mock-key")),
            patch("src.storage.get_storage", return_value=self.storage),
        ):
            res = _persist_configs_to_storage({"sources.yaml": "categories: []\n"})
        self.assertFalse(res["success"])
        self.assertFalse(res["db_saved"])
        self.assertFalse(res["is_cloud_db"])
        self.assertIn("Turso Cloud-Datenbank nicht erreichbar", res["error"] or "")

    def test_turso_auto_recovery_and_readback_verification(self) -> None:
        """Prüft Turso Auto-Recovery (`force_reconnect=True`) und verpflichtende Read-Back-Verifikation."""
        reset_storage_singleton()
        try:
            with (
                patch("src.storage.get_turso_config", return_value=("libsql://mock.turso.io", "mock-key")),
                patch("src.storage.TursoStorage.init_db", side_effect=RuntimeError("transient error")),
            ):
                fallback_db = get_storage(prefer_turso=True)
                self.assertIsInstance(fallback_db, SqliteStorage)
                self.assertFalse(is_cloud_storage(fallback_db))

            with (
                patch("src.storage.get_turso_config", return_value=("libsql://mock.turso.io", "mock-key")),
                patch("src.storage.TursoStorage.init_db", return_value=None),
            ):
                recovered_db = get_storage(prefer_turso=True, force_reconnect=True)
                self.assertIsInstance(recovered_db, TursoStorage)
                self.assertTrue(is_cloud_storage(recovered_db))
        finally:
            reset_storage_singleton()

        # Erfolgreiches Read-Back auf TursoStorage
        turso_mem: dict[str, str] = {}
        fake_turso = TursoStorage("libsql://mock.turso.io", "mock-token")
        with (
            patch.object(fake_turso, "get_metadata_many", side_effect=lambda keys: {k: turso_mem[k] for k in keys if k in turso_mem}),
            patch.object(fake_turso, "set_metadata_many", side_effect=lambda items: turso_mem.update(items)),
        ):
            ok_res = _persist_configs_to_storage({"sources.yaml": "categories: []\n"}, storage=fake_turso)
        self.assertTrue(ok_res["success"])
        self.assertTrue(ok_res["db_saved"])
        self.assertTrue(ok_res["db_verified"])
        self.assertTrue(ok_res["is_cloud_db"])

        # Fehlgeschlagenes Read-Back -> strikt success=False
        with (
            patch.object(fake_turso, "get_metadata_many", return_value={}),
            patch.object(fake_turso, "set_metadata_many", return_value=None),
        ):
            bad_res = _persist_configs_to_storage({"sources.yaml": "categories: []\n"}, storage=fake_turso)
        self.assertFalse(bad_res["success"])
        self.assertFalse(bad_res["db_saved"])
        self.assertFalse(bad_res["db_verified"])
        self.assertIn("Read-Back", bad_res["error"] or "")


class TestHardenedGitHubRssSync(unittest.TestCase):
    """Prüft RSS-only GitHub-Sync (Redirect, 422-Retry, 401/403 Short-Circuit, Token-Sanitization)."""

    def test_sanitize_github_token_strips_quotes_and_auth_prefixes(self) -> None:
        self.assertEqual(_sanitize_github_token('  "ghp_abc123"  '), "ghp_abc123")
        self.assertEqual(_sanitize_github_token("'github_pat_xyz'"), "github_pat_xyz")
        self.assertEqual(_sanitize_github_token("Bearer ghp_bearer_token"), "ghp_bearer_token")
        self.assertEqual(_sanitize_github_token("token ghp_classic_token"), "ghp_classic_token")
        self.assertEqual(_sanitize_github_token(None), "")

    def test_http_401_and_403_short_circuit_without_extra_calls_or_token_leak(self) -> None:
        for status_code in (401, 403):
            resp = MagicMock()
            resp.status_code = status_code
            resp.history = []

            with (
                patch(
                    "src.sources_manager.get_github_sync_config",
                    return_value={"token": "ghp_secret_must_not_leak", "repo": "hschenke/news-bot", "branch": "main"},
                ),
                patch(
                    "src.sources_manager._collect_rss_tree_elements",
                    return_value=[{"path": "static/rss/all.xml", "mode": "100644", "type": "blob", "content": "<rss/>"}],
                ),
                patch("src.sources_manager.requests.get", return_value=resp) as mock_get,
                patch("src.sources_manager.requests.put") as mock_put,
            ):
                result = sync_sources_to_github(config_dict=None, include_rss_feeds=True)

            self.assertFalse(result["success"])
            self.assertEqual(result["status_code"], status_code)
            self.assertIn(f"HTTP {status_code}", result["error"])
            self.assertNotIn("ghp_secret_must_not_leak", result["error"])
            self.assertEqual(mock_get.call_count, 1)
            mock_put.assert_not_called()

    def test_rss_sync_resolves_renamed_repo_retries_422_and_pushes_only_rss_files(self) -> None:
        patch_urls: list[str] = []
        patch_attempt = {"count": 0}

        def fake_get(url: str, headers: dict, timeout: int = 8) -> MagicMock:
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
                paths = [el["path"] for el in json["tree"]]
                self.assertTrue(paths and all(p.startswith("static/rss/") for p in paths))
                resp.json.return_value = {"sha": "new_tree_sha"}
            else:
                resp.json.return_value = {"sha": "new_commit_sha"}
            return resp

        def fake_patch(url: str, headers: dict, json: dict, timeout: int = 10) -> MagicMock:
            patch_urls.append(url)
            patch_attempt["count"] += 1
            resp = MagicMock()
            resp.status_code = 422 if patch_attempt["count"] == 1 else 200
            return resp

        with (
            patch(
                "src.sources_manager.get_github_sync_config",
                return_value={"token": "ghp_valid_test_token", "repo": "hschenke/news-aggregator-bot", "branch": "main"},
            ),
            patch(
                "src.sources_manager._collect_rss_tree_elements",
                return_value=[{"path": "static/rss/all.xml", "mode": "100644", "type": "blob", "content": "<rss/>"}],
            ),
            patch("src.sources_manager.requests.get", side_effect=fake_get),
            patch("src.sources_manager.requests.post", side_effect=fake_post),
            patch("src.sources_manager.requests.patch", side_effect=fake_patch),
            patch("src.sources_manager.purge_jsdelivr_cache", return_value={"success": True, "purged_count": 1, "errors": []}) as mock_purge,
        ):
            result = sync_sources_to_github(
                config_dict=None,
                commit_message="chore(rss): test",
                include_rss_feeds=True,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["repo"], "hschenke/news-bot")
        self.assertEqual(result["commit_sha"], "new_commit_sha")
        self.assertEqual(len(patch_urls), 2)
        self.assertTrue(all("hschenke/news-bot" in u for u in patch_urls))
        mock_purge.assert_called_once()


class TestBatchMetadataAndDeletedFeedCleanup(unittest.TestCase):
    """Prüft Batch-Metadaten (SQLite & Turso) sowie das Bereinigen gelöschter Feed-Artikel."""

    def test_sqlite_batch_metadata_and_delete_by_source_url(self) -> None:
        storage = SqliteStorage(":memory:")
        storage.init_db()
        try:
            storage.set_metadata_many({"k1": "v1", "k2": "v2"})
            loaded = storage.get_metadata_many(["k1", "k2", "k3"])
            self.assertEqual(loaded, {"k1": "v1", "k2": "v2"})

            storage.save_articles(
                [
                    Article(title="Keep", link="https://ex.com/keep", source="KeepFeed", source_url="https://ex.com/keep.xml", category="Tech"),
                    Article(title="Drop 1", link="https://ex.com/drop1", source="DropFeed", source_url="https://ex.com/drop.xml", category="Tech"),
                    Article(title="Drop 2", link="https://ex.com/drop2", source="DropFeed", source_url="https://ex.com/drop.xml", category="Tech"),
                ]
            )
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


class TestImmediateSaveAndRollbackE2E(unittest.TestCase):
    """Prüft Sofort-Speichern ohne Entwurfs-Sammlung, Fail-Closed-Rollback auf alte Werte, Overlay und Workflows."""

    def test_get_news_data_filters_out_deleted_feed_source_urls(self) -> None:
        """Artikel aus gelöschten Feeds dürfen nicht aus der DB in Artikel/KI-Tab zurückkehren."""
        import time
        import streamlit as st
        from src.ui.state import get_news_data

        now = time.time()
        mock_cfg = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [{"name": "Active Feed", "url": "https://active-feed.example.com/rss.xml"}],
                }
            ]
        }
        articles = [
            Article(
                title="Aktiver Feed Artikel",
                link="https://ex.com/active-1",
                category="Tech & AI",
                source="Active Feed",
                source_url="https://active-feed.example.com/rss.xml",
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
            patch("src.ui.state.load_sources", return_value=mock_cfg),
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

    def test_workflows_are_streamlined_rss_only_with_turso_db(self) -> None:
        """Workflows laden Konfiguration direkt aus Turso DB und committen ausschließlich static/rss/."""
        root = Path(__file__).resolve().parent.parent
        daily = (root / ".github" / "workflows" / "daily_digest.yaml").read_text(encoding="utf-8")
        update_rss = (root / ".github" / "workflows" / "update_rss.yaml").read_text(encoding="utf-8")
        styles_py = (root / "src" / "ui" / "styles.py").read_text(encoding="utf-8")

        for wf in (daily, update_rss):
            self.assertIn("TURSO_URL: ${{ secrets.TURSO_URL }}", wf)
            self.assertIn("TURSO_KEY: ${{ secrets.TURSO_KEY }}", wf)
            self.assertIn("git add static/rss/", wf)
            self.assertNotIn("config/sources.yaml", wf)
            self.assertNotIn("config/settings.yaml", wf)
            self.assertNotIn("config/prompts.yaml", wf)
            self.assertNotIn("mark_canonical_configs_synced_to_git", wf)
            self.assertIn("group: news-bot-main-sync", wf)
            self.assertIn("cancel-in-progress: false", wf)
            self.assertIn("git rebase -X theirs origin/main", wf)

        # Graues Lade-Overlay muss für alle Sofort-Speichern-Aktionen greifen
        for btn_key in (
            "btn_add_cat",
            "btn_ren_cat",
            "del_cat_",
            "btn_submit_new_feed",
            "btn_save_feed_",
            "del_feed_",
            "btn_apply_settings",
            "btn_save_ki_prompts",
            "btn_reset_ki_main_prompt",
        ):
            self.assertIn(btn_key, styles_py)

    def test_manage_tab_e2e_immediate_save_success_on_every_action(self) -> None:
        """
        End-to-End Streamlit AppTest (Alles Grün):
        - Feed löschen -> sofort gespeichert!
        - Neuen Feed anlegen -> sofort gespeichert!
        - Bestehenden Feed bearbeiten ('💾 Feed speichern') -> sofort gespeichert!
        - Globale Einstellung ändern ('💾 Einstellungen speichern') -> sofort gespeichert!
        - Kein Sammel-/Entwurfs-Balken mehr!
        """
        from streamlit.testing.v1 import AppTest

        def _run_manage() -> None:
            import streamlit as st
            from src.ui.tabs.manage_tab import render_manage_tab

            render_manage_tab(st.session_state["working_sources_config"], is_admin=True)

        db_state: dict[str, dict] = {
            "cfg": {
                "categories": [
                    {
                        "name": "Tech & AI",
                        "feeds": [
                            {"name": "Feed Alpha", "url": "https://example.com/1.xml"},
                            {"name": "Feed Beta", "url": "https://example.com/2.xml"},
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
        }
        save_calls: list[dict] = []

        def fake_save_sources(cfg: dict, **kwargs: object) -> dict:
            self.assertFalse(kwargs.get("sync_github", True))
            self.assertTrue(kwargs.get("persist_db", False))
            db_state["cfg"] = copy.deepcopy(cfg)
            save_calls.append(copy.deepcopy(cfg))
            return {
                "success": True,
                "db_saved": True,
                "db_verified": True,
                "is_cloud_db": True,
                "storage_backend": "turso",
                "error": None,
            }

        with (
            patch("src.ui.tabs.manage_tab.save_sources", side_effect=fake_save_sources),
            patch("src.ui.tabs.manage_tab.load_sources", side_effect=lambda: copy.deepcopy(db_state["cfg"])),
            patch("src.ui.tabs.manage_tab.load_prompts", return_value={"custom_main_prompt": "Keep Prompt"}),
            patch("src.ui.tabs.manage_tab.fetch_feed_items", return_value=[]),
            patch("src.ui.tabs.manage_tab.run_in_background"),
        ):
            at = AppTest.from_function(_run_manage, default_timeout=20)
            at.session_state["working_sources_config"] = copy.deepcopy(db_state["cfg"])
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual([b for b in at.button if b.key in ("top_save_sources_btn", "top_discard_sources_btn")], [])

            # 1. Feed löschen -> sofort gespeichert (1. DB-Save)
            del_btns = [b for b in at.button if (b.key or "").startswith("del_feed_")]
            self.assertEqual(len(del_btns), 2)
            del_btns[0].click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(save_calls), 1)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "success")

            # 2. Neuen Feed anlegen -> sofort gespeichert (2. DB-Save)
            next(ti for ti in at.text_input if ti.key == "inp_new_feed_url").set_value("https://example.com/fresh.xml")
            next(ti for ti in at.text_input if ti.key == "inp_new_feed_name").set_value("Neuer Feed")
            next(b for b in at.button if b.key == "btn_submit_new_feed").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(save_calls), 2)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "success")

            # 3. Feed umbenennen & '💾 Feed speichern' -> sofort gespeichert (3. DB-Save)
            name_inps = [ti for ti in at.text_input if (ti.key or "").startswith("ed_name_")]
            save_feed_btns = [b for b in at.button if (b.key or "").startswith("btn_save_feed_")]
            name_inps[0].set_value("Umbenannter Feed")
            save_feed_btns[0].click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(save_calls), 3)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "success")

            # 4. Globale Einstellung ändern & '💾 Einstellungen speichern' -> sofort gespeichert (4. DB-Save)
            next(ni for ni in at.number_input if ni.key == "inp_set_retention_days").set_value(22)
            next(b for b in at.button if b.key == "btn_apply_settings").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(save_calls), 4)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "success")

        final_cfg = db_state["cfg"]
        final_urls = {f["url"] for f in final_cfg["categories"][0]["feeds"]}
        final_names = {f["name"] for f in final_cfg["categories"][0]["feeds"]}
        self.assertEqual(final_urls, {"https://example.com/2.xml", "https://example.com/fresh.xml"})
        self.assertIn("Umbenannter Feed", final_names)
        self.assertEqual(final_cfg["settings"]["archive_retention_days"], 22)
        self.assertEqual(final_cfg["settings"]["custom_main_prompt"], "Keep Prompt")

    def test_manage_tab_e2e_failed_save_shows_error_and_reloads_old_values(self) -> None:
        """
        End-to-End Fail-Closed Test (NICHT grün -> IMMER Fehlermeldung + Reload mit alten Werten!):
        1. Feed löschen schlägt fehl -> rotes ❌-Banner UND Feed bleibt unverändert vorhanden!
        2. Feed umbenennen schlägt fehl -> rotes ❌-Banner UND Textfeld zeigt wieder den alten Namen!
        3. Einstellung ändern schlägt fehl -> rotes ❌-Banner UND Eingabefeld springt auf alten Wert (8) zurück!
        """
        from streamlit.testing.v1 import AppTest

        def _run_manage() -> None:
            import streamlit as st
            from src.ui.tabs.manage_tab import render_manage_tab

            render_manage_tab(st.session_state["working_sources_config"], is_admin=True)

        persisted_old = {
            "categories": [
                {
                    "name": "Tech & AI",
                    "feeds": [{"name": "Alter Feed", "url": "https://example.com/feed.xml"}],
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
        fail_result = {
            "success": False,
            "db_saved": False,
            "db_verified": False,
            "is_cloud_db": False,
            "storage_backend": "sqlite",
            "error": "Turso Cloud-Datenbank nicht erreichbar",
        }

        with (
            patch("src.ui.tabs.manage_tab.save_sources", return_value=fail_result),
            patch("src.ui.tabs.manage_tab.load_sources", side_effect=lambda: copy.deepcopy(persisted_old)),
            patch("src.ui.tabs.manage_tab.load_prompts", return_value={}),
            patch("src.ui.tabs.manage_tab.fetch_feed_items", return_value=[]),
            patch("src.ui.tabs.manage_tab.run_in_background"),
        ):
            at = AppTest.from_function(_run_manage, default_timeout=20)
            at.session_state["working_sources_config"] = copy.deepcopy(persisted_old)
            at.run()

            # 1. Feed löschen schlägt fehl -> Feed muss noch da sein!
            next(b for b in at.button if (b.key or "").startswith("del_feed_")).click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "error")
            self.assertEqual(at.session_state.get("_notice_icon_manage_notice"), "❌")
            self.assertIn("alte Werte wurden wiederhergestellt", str(at.session_state.get("manage_notice", "")))
            self.assertEqual(len(at.session_state["working_sources_config"]["categories"][0]["feeds"]), 1)

            # 2. Feed umbenennen schlägt fehl -> Textfeld muss wieder "Alter Feed" zeigen!
            ed_name = next(ti for ti in at.text_input if (ti.key or "").startswith("ed_name_"))
            ed_name.set_value("Ungültiger Neuer Name")
            next(b for b in at.button if (b.key or "").startswith("btn_save_feed_")).click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "error")
            reloaded_name = next(ti for ti in at.text_input if (ti.key or "").startswith("ed_name_")).value
            self.assertEqual(reloaded_name, "Alter Feed")

            # 3. Globale Einstellung ändern schlägt fehl -> Zahl muss wieder auf 8 zurückspringen!
            next(ni for ni in at.number_input if ni.key == "inp_set_retention_days").set_value(42)
            next(b for b in at.button if b.key == "btn_apply_settings").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state.get("_notice_level_manage_notice"), "error")
            reloaded_ret = next(ni for ni in at.number_input if ni.key == "inp_set_retention_days").value
            self.assertEqual(reloaded_ret, 8)

    def test_ki_tab_e2e_prompt_save_failure_reloads_old_prompt(self) -> None:
        """Wenn das Speichern von KI-Prompts fehlschlägt, erscheint ein rotes ❌-Banner und der alte Prompt wird neu geladen."""
        from streamlit.testing.v1 import AppTest

        def _run_ki() -> None:
            from src.ui.tabs.ki_tab import render_ki_tab

            render_ki_tab({"Tech & AI": []}, is_admin=True)

        old_prompts = {
            "custom_main_prompt": "Alter stabiler Hauptprompt",
            "custom_prompt_directives": "Alte Direktive",
        }
        fail_res = {"success": False, "db_saved": False, "db_verified": False, "error": "DB Read-Back fehlgeschlagen"}

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=False),
            patch("src.ui.tabs.ki_tab.load_prompts", return_value=copy.deepcopy(old_prompts)),
            patch("src.ui.tabs.ki_tab.save_prompts", return_value=fail_res),
        ):
            at = AppTest.from_function(_run_ki, default_timeout=20)
            at.session_state["cached_summary"] = "## Test"
            at.run()
            self.assertFalse(at.exception)

            next(ta for ta in at.text_area if ta.key == "input_ki_main_prompt").set_value("Geänderter fehlerhafter Prompt")
            next(b for b in at.button if b.key == "btn_save_ki_prompts").click().run()
            self.assertFalse(at.exception)

        self.assertEqual(at.session_state.get("_notice_level_ki_notice"), "error")
        self.assertEqual(at.session_state.get("_notice_icon_ki_notice"), "❌")
        self.assertIn("alte Prompts wurden wiederhergestellt", str(at.session_state.get("ki_notice", "")))
        reloaded_prompt = next(ta for ta in at.text_area if ta.key == "input_ki_main_prompt").value
        self.assertEqual(reloaded_prompt, "Alter stabiler Hauptprompt")

    def test_streamlit_app_url_only_from_environment_variable(self) -> None:
        """STREAMLIT_APP_URL darf ausschließlich aus der Umgebungsvariable gelesen werden."""
        import os
        from src.summarizer import get_streamlit_app_url

        with patch.dict("os.environ", {"STREAMLIT_APP_URL": "https://holli-news-bot.streamlit.app/"}, clear=False):
            self.assertEqual(get_streamlit_app_url(), "https://holli-news-bot.streamlit.app")

        env_without_url = {k: v for k, v in os.environ.items() if k != "STREAMLIT_APP_URL"}
        with patch.dict("os.environ", env_without_url, clear=True):
            self.assertEqual(get_streamlit_app_url(), "")


if __name__ == "__main__":
    unittest.main()
