"""
Unit- & UI-Tests für die ausschließliche `ai_brain`-Anbindung (1-Secret Plug-&-Play,
`tier="auto"` Routing, fail-closed Fehlerverhalten ohne Fallback und AI-Brain Optionen in `ki_tab.py`).
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_brain import (
    AppBrainPolicy,
    BrainAuthError,
    BrainGenerationResult,
    BrainProviderError,
    BrainTier,
)
from streamlit.testing.v1 import AppTest

from src.summarizer import (
    DEFAULT_SYSTEM_INSTRUCTION,
    BriefingResult,
    get_last_briefing_result,
    load_ai_brain_policy,
    save_ai_brain_policy,
    summarize_news_with_gemini,
)

SAMPLE_NEWS: dict[str, list[dict[str, object]]] = {
    "Tech & AI": [
        {
            "title": "Neues Open-Source-LLM veröffentlicht",
            "link": "https://example.com/llm-release",
            "summary": "Das Modell erreicht hohe Benchmarks bei geringem Speicherbedarf.",
            "source": "Heise Online News",
            "timestamp": 1728000100.0,
            "feedback": 1,
        }
    ],
}


def _make_mock_brain(
    *,
    text: str = "## 🤖 Tech & AI\n- **[Neues Open-Source-LLM veröffentlicht](https://example.com/llm-release)**: Hohe Benchmarks bei geringem Speicherbedarf.",
    model_used: str = "gemini-2.5-flash",
    tier: BrainTier = BrainTier.STANDARD,
    policy: AppBrainPolicy | None = None,
) -> MagicMock:
    """Erstellt ein Context-Manager-Mock für `ai_brain.connect()`."""
    brain = MagicMock()
    brain.__enter__.return_value = brain
    brain.__exit__.return_value = None
    brain.generate_text.return_value = BrainGenerationResult(
        text=text,
        tier=tier,
        model_used=model_used,
    )
    brain.get_app_policy.return_value = policy or AppBrainPolicy(
        llm_tier=BrainTier.STANDARD,
        search_tier=BrainTier.FAST,
        allow_paid=False,
        maps_mode="off",
    )
    brain.set_app_policy.side_effect = lambda p: p
    return brain


class TestAiBrainSummarizerIntegration(unittest.TestCase):
    """Prüft `summarize_news_with_gemini` im reinen `ai_brain`-Betrieb ohne Fallback."""

    def test_summarize_uses_ai_brain_tier_auto_and_returns_model_metadata(self) -> None:
        mock_brain = _make_mock_brain(model_used="gemini-2.5-flash", tier=BrainTier.STANDARD)

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain) as mock_connect,
            self.assertLogs("src.summarizer", level=logging.INFO) as log_cm,
        ):
            summary_md = summarize_news_with_gemini(SAMPLE_NEWS)

        mock_connect.assert_called_once_with()
        mock_brain.generate_text.assert_called_once()
        args, kwargs = mock_brain.generate_text.call_args
        self.assertIn("Neues Open-Source-LLM veröffentlicht", args[0])
        self.assertEqual(kwargs["tier"], "auto")
        self.assertEqual(kwargs["system_instruction"], DEFAULT_SYSTEM_INSTRUCTION)
        self.assertIsNone(kwargs["allow_paid"])

        self.assertIsInstance(summary_md, str)
        self.assertIn("Neues Open-Source-LLM veröffentlicht", summary_md)
        self.assertIn("https://example.com/llm-release", summary_md)

        meta = get_last_briefing_result()
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.backend, "ai_brain")
        self.assertEqual(meta.model_used, "gemini-2.5-flash")
        self.assertEqual(meta.tier_used, "standard")
        self.assertEqual(meta.display_label, "gemini-2.5-flash · AI-Brain (Standard)")
        self.assertTrue(any("gemini-2.5-flash" in msg and "standard" in msg for msg in log_cm.output))

    def test_summarize_return_result_true_returns_briefing_result_dataclass(self) -> None:
        mock_brain = _make_mock_brain(model_used="llama-3.3-70b-versatile", tier=BrainTier.DEEP)

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
        ):
            res = summarize_news_with_gemini(SAMPLE_NEWS, return_result=True)

        self.assertIsInstance(res, BriefingResult)
        self.assertEqual(res.backend, "ai_brain")
        self.assertEqual(res.model_used, "llama-3.3-70b-versatile")
        self.assertEqual(res.tier_used, "deep")
        self.assertEqual(res.display_label, "llama-3.3-70b-versatile · AI-Brain (Deep)")
        self.assertIn("Neues Open-Source-LLM veröffentlicht", res.markdown)

    def test_raises_brain_auth_error_when_not_configured(self) -> None:
        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=False),
            patch("src.summarizer.ai_brain.connect") as mock_connect,
            self.assertRaises(BrainAuthError),
        ):
            summarize_news_with_gemini(SAMPLE_NEWS, return_result=True)

        mock_connect.assert_not_called()

    def test_propagates_brain_provider_error_without_fallback(self) -> None:
        mock_brain = _make_mock_brain()
        mock_brain.generate_text.side_effect = BrainProviderError("Alle Provider ausgelastet")

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
            self.assertLogs("src.summarizer", level=logging.ERROR),
            self.assertRaises(BrainProviderError),
        ):
            summarize_news_with_gemini(SAMPLE_NEWS, return_result=True)

    def test_raises_brain_provider_error_on_empty_response(self) -> None:
        mock_brain = _make_mock_brain(text="   ")

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
            self.assertRaises(BrainProviderError),
        ):
            summarize_news_with_gemini(SAMPLE_NEWS, return_result=True)


class TestAiBrainPolicyPersistence(unittest.TestCase):
    """Prüft Laden, Validieren und Speichern der `AppBrainPolicy` über `ai_brain`."""

    def test_load_policy_returns_news_bot_defaults_when_offline(self) -> None:
        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=False),
            patch("src.summarizer.ai_brain.connect") as mock_connect,
        ):
            policy, err = load_ai_brain_policy()

        mock_connect.assert_not_called()
        self.assertIsNone(err)
        self.assertEqual(policy.llm_tier, BrainTier.STANDARD)
        self.assertEqual(policy.search_tier, BrainTier.FAST)
        self.assertFalse(policy.allow_paid)
        self.assertEqual(policy.maps_mode, "off")

    def test_load_policy_reads_from_brain_when_online(self) -> None:
        stored = AppBrainPolicy(
            llm_tier=BrainTier.DEEP,
            search_tier=BrainTier.STANDARD,
            allow_paid=True,
            maps_mode="off",
        )
        mock_brain = _make_mock_brain(policy=stored)

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
        ):
            policy, err = load_ai_brain_policy()

        self.assertIsNone(err)
        self.assertEqual(policy, stored)
        mock_brain.get_app_policy.assert_called_once_with()

    def test_load_policy_handles_brain_error_with_safe_defaults(self) -> None:
        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", side_effect=BrainAuthError("Auth fehlgeschlagen")),
            self.assertLogs("src.summarizer", level=logging.WARNING),
        ):
            policy, err = load_ai_brain_policy()

        self.assertEqual(err, "Auth fehlgeschlagen")
        self.assertEqual(policy.llm_tier, BrainTier.STANDARD)
        self.assertEqual(policy.search_tier, BrainTier.FAST)
        self.assertFalse(policy.allow_paid)
        self.assertEqual(policy.maps_mode, "off")

    def test_save_policy_persists_all_tiers_and_maps_mode_off(self) -> None:
        for llm_t, search_t, allow_paid in (
            (BrainTier.FAST, BrainTier.FAST, False),
            (BrainTier.STANDARD, BrainTier.DEEP, False),
            ("deep", "standard", True),
        ):
            mock_brain = _make_mock_brain()
            with (
                patch("src.summarizer.ai_brain.is_configured", return_value=True),
                patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
            ):
                saved = save_ai_brain_policy(llm_tier=llm_t, search_tier=search_t, allow_paid=allow_paid)

            expected = AppBrainPolicy(
                llm_tier=BrainTier(str(llm_t)),
                search_tier=BrainTier(str(search_t)),
                allow_paid=allow_paid,
                maps_mode="off",
            )
            mock_brain.set_app_policy.assert_called_once_with(expected)
            self.assertEqual(saved, expected)

    def test_save_policy_rejects_invalid_tier(self) -> None:
        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            self.assertRaises(ValueError),
        ):
            save_ai_brain_policy(llm_tier="ultra", search_tier="fast", allow_paid=False)


class TestKiTabAiBrainOptionsUi(unittest.TestCase):
    """Prüft '⚙️ AI-Brain Optionen' und den alleinstehenden Generieren-Button in `ki_tab.py`."""

    @staticmethod
    def _run_ki_tab_script() -> None:
        import streamlit as st

        from src.ui.tabs.ki_tab import render_ki_tab

        is_admin = bool(st.session_state.get("_test_is_admin", True))
        render_ki_tab({"Tech & AI": []}, is_admin=is_admin)

    def test_ki_tab_shows_offline_badge_and_disables_generate_button(self) -> None:
        with patch("src.summarizer.ai_brain.is_configured", return_value=False):
            at = AppTest.from_function(self._run_ki_tab_script, default_timeout=20)
            at.session_state["_test_is_admin"] = True
            at.session_state["cached_summary"] = "## Tech & AI\n- **[A](https://example.com)**: Ok"
            at.run()

        self.assertFalse(at.exception, f"Unexpected UI exception: {at.exception}")
        md_texts = " ".join(m.value for m in at.markdown)
        self.assertIn("⚪ AI-Brain Offline", md_texts)
        self.assertNotIn("Fallback (lokaler API-Key)", md_texts)
        save_btns = [b for b in at.button if b.key == "btn_save_ai_brain_policy"]
        self.assertEqual(len(save_btns), 0)

        # Standalone generate button exists below expanders, disabled when AI-Brain is offline
        gen_btn = next(b for b in at.button if b.key == "btn_generate_briefing")
        self.assertTrue(gen_btn.disabled)
        # Only 2 expanders exist (AI-Brain Optionen & KI-Prompt-Konfiguration)
        self.assertEqual(len(at.expander), 2)

    def test_ki_tab_online_admin_can_load_and_save_policy(self) -> None:
        initial_policy = AppBrainPolicy(
            llm_tier=BrainTier.STANDARD,
            search_tier=BrainTier.FAST,
            allow_paid=False,
            maps_mode="off",
        )
        mock_brain = _make_mock_brain(policy=initial_policy)

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
        ):
            at = AppTest.from_function(self._run_ki_tab_script, default_timeout=20)
            at.session_state["_test_is_admin"] = True
            at.session_state["cached_summary"] = "## Tech & AI\n- **[A](https://example.com)**: Ok"
            at.run()

            self.assertFalse(at.exception, f"Unexpected UI exception: {at.exception}")
            md_texts = " ".join(m.value for m in at.markdown)
            self.assertIn("🟢 AI-Brain Online", md_texts)
            mock_brain.get_app_policy.assert_called_once()
            self.assertEqual(len(at.expander), 2)

            gen_btn = next(b for b in at.button if b.key == "btn_generate_briefing")
            self.assertFalse(gen_btn.disabled)

            llm_sel = next(s for s in at.selectbox if s.key == "sel_ai_brain_llm_tier")
            search_sel = next(s for s in at.selectbox if s.key == "sel_ai_brain_search_tier")
            paid_chk = next(c for c in at.checkbox if c.key == "chk_ai_brain_allow_paid")
            self.assertEqual(llm_sel.value, BrainTier.STANDARD)
            self.assertEqual(search_sel.value, BrainTier.FAST)
            self.assertFalse(paid_chk.value)

            llm_sel.set_value(BrainTier.DEEP)
            search_sel.set_value(BrainTier.STANDARD)
            paid_chk.set_value(True)

            save_btn = next(b for b in at.button if b.key == "btn_save_ai_brain_policy")
            save_btn.click().run()

        self.assertFalse(at.exception, f"Unexpected UI exception on save: {at.exception}")
        mock_brain.set_app_policy.assert_called_once_with(
            AppBrainPolicy(
                llm_tier=BrainTier.DEEP,
                search_tier=BrainTier.STANDARD,
                allow_paid=True,
                maps_mode="off",
            )
        )
        self.assertIn("Deep", str(at.session_state["ki_notice"]))
        self.assertIn("Paid", str(at.session_state["ki_notice"]))

    def test_ki_tab_non_admin_cannot_edit_save_or_generate(self) -> None:
        mock_brain = _make_mock_brain()

        with (
            patch("src.summarizer.ai_brain.is_configured", return_value=True),
            patch("src.summarizer.ai_brain.connect", return_value=mock_brain),
        ):
            at = AppTest.from_function(self._run_ki_tab_script, default_timeout=20)
            at.session_state["_test_is_admin"] = False
            at.session_state["cached_summary"] = "## Tech & AI\n- **[A](https://example.com)**: Ok"
            at.run()

        self.assertFalse(at.exception, f"Unexpected UI exception: {at.exception}")
        llm_sel = next(s for s in at.selectbox if s.key == "sel_ai_brain_llm_tier")
        search_sel = next(s for s in at.selectbox if s.key == "sel_ai_brain_search_tier")
        paid_chk = next(c for c in at.checkbox if c.key == "chk_ai_brain_allow_paid")
        self.assertTrue(llm_sel.disabled)
        self.assertTrue(search_sel.disabled)
        self.assertTrue(paid_chk.disabled)
        self.assertEqual(len([b for b in at.button if b.key == "btn_save_ai_brain_policy"]), 0)
        self.assertEqual(len([b for b in at.button if b.key == "btn_generate_briefing"]), 0)


class TestAiBrainOnlyArchitecture(unittest.TestCase):
    """Prüft, dass keine Gemini-Fallback-Reste mehr in Code, Dependencies & Workflow existieren."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent

    def test_summarizer_has_no_direct_gemini_or_fallback_code(self) -> None:
        summarizer_src = (self.root / "src" / "summarizer.py").read_text(encoding="utf-8")
        for forbidden in (
            "google.genai",
            "from google import",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
            "AVAILABLE_GEMINI_MODELS",
            "get_candidate_models",
            "_generate_via_direct_gemini",
            "_generate_fallback_summary",
        ):
            self.assertNotIn(forbidden, summarizer_src)

    def test_requirements_files_use_bot_dashboard_without_direct_google_genai(self) -> None:
        expected = "bot-dashboard @ git+https://github.com/hschenke/bot-dashboard.git@main"
        req = (self.root / "requirements.txt").read_text(encoding="utf-8")
        req_bot = (self.root / "requirements-bot.txt").read_text(encoding="utf-8")
        self.assertIn(expected, req)
        self.assertIn(expected, req_bot)
        self.assertNotIn("google-genai", req)
        self.assertNotIn("google-genai", req_bot)

    def test_daily_digest_workflow_uses_ai_brain_only(self) -> None:
        workflow = (self.root / ".github" / "workflows" / "daily_digest.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'pip install --upgrade --no-cache-dir "bot-dashboard @ git+https://github.com/hschenke/bot-dashboard.git@main"',
            workflow,
        )
        self.assertIn("AI_BRAIN_KEY: ${{ secrets.AI_BRAIN_KEY }}", workflow)
        self.assertNotIn("GEMINI_API_KEY", workflow)
        self.assertNotIn("GEMINI_MODEL", workflow)


if __name__ == "__main__":
    unittest.main()
