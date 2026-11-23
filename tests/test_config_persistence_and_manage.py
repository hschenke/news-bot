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
    _sha256_text,
    invalidate_config_sync_cache,
    load_sources,
    save_sources,
    sync_configs_from_storage,
    sync_sources_to_github,
)
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


class TestHardenedGitHubSync(unittest.TestCase):
    """Prüft Redirect-Auflösung (Repo-Rename), 422-Konflikt-Retry und Config-only Commits."""

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

        # 2. Ehrliche Notice-Formatierung für DB+GitHub vs. DB-only
        icon1, msg1 = _build_save_notice({"db_saved": True, "success": True}, newly_fetched_count=3)
        self.assertEqual(icon1, "✅")
        self.assertIn("Cloud-Datenbank & GitHub", msg1)
        self.assertIn("3 neue Artikel", msg1)

        icon2, msg2 = _build_save_notice({"db_saved": True, "success": False, "error": "Kein GITHUB_TOKEN hinterlegt."})
        self.assertEqual(icon2, "✅")
        self.assertIn("Cloud-Datenbank gespeichert", msg2)
        self.assertIn("Kein GITHUB_TOKEN", msg2)

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
        """Daily Digest & Update RSS Workflows müssen Cloud-DB-Konfigurationen synchronisieren und committen."""
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
            return {"db_saved": True, "success": True, "commit_sha": "abc1234"}

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
        self.assertIn("Cloud-Datenbank & GitHub", str(at.session_state.get("manage_notice", "")))

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
