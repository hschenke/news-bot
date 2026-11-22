"""
KI Briefing tab module for News Bot UI.
Renders AI-curated daily briefings and provides AI-Brain options, prompt configuration,
and a direct briefing generation trigger for administrators.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import streamlit as st
from ai_brain import AppBrainPolicy, BrainError, BrainTier, default_app_policy

from src.aggregator import (
    load_prompts,
    save_prompts,
    sync_sources_to_github,
)
from src.rss_generator import export_briefing_rss
from src.storage import get_storage
from src.summarizer import (
    DEFAULT_DIRECTIVES,
    DEFAULT_MAIN_PROMPT_TEMPLATE,
    is_ai_brain_configured,
    load_ai_brain_policy,
    save_ai_brain_policy,
    summarize_news_with_gemini,
)
from src.ui.styles import render_dismissible_notice, set_dismissible_notice

logger = logging.getLogger(__name__)

_TIER_OPTIONS: tuple[BrainTier, ...] = (
    BrainTier.FAST,
    BrainTier.STANDARD,
    BrainTier.DEEP,
)
_TIER_LABELS: dict[BrainTier, str] = {
    BrainTier.FAST: "Fast",
    BrainTier.STANDARD: "Standard",
    BrainTier.DEEP: "Deep",
}
_POLICY_CACHE_TTL_SECONDS: float = 60.0


def _get_cached_ai_brain_policy(force_reload: bool = False) -> tuple[AppBrainPolicy, str | None]:
    """Lädt die `AppBrainPolicy` mit kurzem Session-Cache, um Tab-Reruns schnell zu halten."""
    if not is_ai_brain_configured():
        return default_app_policy("news-bot"), None

    now = time.monotonic()
    cached_policy = st.session_state.get("_ai_brain_policy")
    cached_ts = float(st.session_state.get("_ai_brain_policy_ts") or 0.0)
    if (
        not force_reload
        and isinstance(cached_policy, AppBrainPolicy)
        and (now - cached_ts) < _POLICY_CACHE_TTL_SECONDS
    ):
        return cached_policy, None

    policy, load_err = load_ai_brain_policy()
    if load_err is None:
        st.session_state["_ai_brain_policy"] = policy
        st.session_state["_ai_brain_policy_ts"] = now
    return policy, load_err


def _render_ai_brain_options(is_admin: bool) -> bool:
    """Rendert den kompakten Expander '⚙️ AI-Brain Optionen' (editierbar für Admins)."""
    online = is_ai_brain_configured()

    with st.expander("⚙️ AI-Brain Optionen", expanded=False):
        if online:
            st.markdown("**Status:** 🟢 AI-Brain Online")
            policy, load_err = _get_cached_ai_brain_policy()
        else:
            st.markdown("**Status:** ⚪ AI-Brain Offline")
            st.caption(
                "`AI_BRAIN_KEY` nicht konfiguriert – hinterlege das Secret in `.env` bzw. "
                "Streamlit Secrets, um AI-Briefings sowie Tiers & Budget zu aktivieren."
            )
            policy = default_app_policy("news-bot")
            load_err = None

        if load_err:
            st.warning(
                f"AI-Brain Policy konnte nicht geladen werden ({load_err}) – zeige Free-Only-Defaults."
            )

        llm_idx = _TIER_OPTIONS.index(policy.llm_tier) if policy.llm_tier in _TIER_OPTIONS else 1
        search_idx = (
            _TIER_OPTIONS.index(policy.search_tier) if policy.search_tier in _TIER_OPTIONS else 0
        )
        controls_disabled = not (is_admin and online)

        col_llm, col_search = st.columns(2)
        with col_llm:
            selected_llm_tier = st.selectbox(
                "AI-Briefing Tier",
                options=list(_TIER_OPTIONS),
                index=llm_idx,
                format_func=lambda tier: _TIER_LABELS[tier],
                key="sel_ai_brain_llm_tier",
                disabled=controls_disabled,
            )
        with col_search:
            selected_search_tier = st.selectbox(
                "Websearch Tier",
                options=list(_TIER_OPTIONS),
                index=search_idx,
                format_func=lambda tier: _TIER_LABELS[tier],
                key="sel_ai_brain_search_tier",
                disabled=controls_disabled,
            )

        allow_paid = st.checkbox(
            "💳 Paid-Modelle bei Bedarf erlauben (allow_paid)",
            value=bool(policy.allow_paid),
            key="chk_ai_brain_allow_paid",
            disabled=controls_disabled,
        )
        budget_label = "Paid" if allow_paid else "Free"
        st.caption(
            f"Budget: **{budget_label}** · Ohne Haken strikt 0 % Kosten (Free-Only). "
            "Streamlit UI & täglicher GitHub Actions Worker nutzen automatisch dieselbe Policy."
        )

        if is_admin and online:
            save_policy_clicked = st.button(
                "💾 AI-Brain Optionen speichern",
                key="btn_save_ai_brain_policy",
                use_container_width=True,
            )
            if save_policy_clicked:
                with st.spinner("Speichere AI-Brain Optionen..."):
                    try:
                        saved = save_ai_brain_policy(
                            llm_tier=selected_llm_tier,
                            search_tier=selected_search_tier,
                            allow_paid=allow_paid,
                        )
                    except BrainError as exc:
                        logger.warning("AI-Brain Optionen konnten nicht gespeichert werden: %s", exc)
                        st.error(f"AI-Brain Optionen konnten nicht gespeichert werden: {exc}")
                    else:
                        st.session_state["_ai_brain_policy"] = saved
                        st.session_state["_ai_brain_policy_ts"] = time.monotonic()
                        saved_budget = "Paid" if saved.allow_paid else "Free"
                        set_dismissible_notice(
                            "ki_notice",
                            (
                                f"AI-Brain Optionen gespeichert: "
                                f"AI-Briefing {_TIER_LABELS[saved.llm_tier]} · "
                                f"Websearch {_TIER_LABELS[saved.search_tier]} · "
                                f"Budget {saved_budget}"
                            ),
                            icon="🧠",
                        )
                        st.rerun()

    return online


def render_ki_tab(
    news_data: dict[str, list[dict[str, Any]]],
    is_admin: bool,
) -> None:
    """Renders the AI briefing tab."""
    st.subheader("🧠 KI-Briefing")
    st.caption("Kuratierte Zusammenfassung der wichtigsten Meldungen.")

    if st.session_state.get("ki_notice"):
        render_dismissible_notice(st.session_state["ki_notice"], notice_key="ki_notice", icon="✨")

    # Load latest briefing from session state or storage
    current_briefing: str | None = st.session_state.get("cached_summary")
    current_model_label: str = str(st.session_state.get("cached_summary_model") or "")

    if not current_briefing:
        try:
            storage = get_storage()
            latest = storage.get_latest_briefing()
            if latest:
                content = latest.get("content_markdown") or latest.get("content")
                if isinstance(content, str) and content.strip():
                    current_briefing = content
                    current_model_label = str(latest.get("model_used") or "")
                    st.session_state["cached_summary"] = current_briefing
                    if current_model_label:
                        st.session_state["cached_summary_model"] = current_model_label
        except Exception as exc:  # noqa: BLE001 - Storage-Ausfall darf UI-Rendering nicht blockieren
            logger.debug("Failed to load latest briefing from storage: %s", exc)

    # Expander 1: AI-Brain Optionen (Fast / Standard / Deep & Free / Paid)
    ai_brain_online = _render_ai_brain_options(is_admin=is_admin)

    # Expander 2: Prompt Configuration (for Admin and read-only for others)
    try:
        prompts_cfg = load_prompts()
    except Exception as exc:  # noqa: BLE001 - Standard-Prompts bei fehlender prompts.yaml
        logger.debug("Failed to load prompts configuration: %s", exc)
        prompts_cfg = {}

    current_main_prompt = (
        (prompts_cfg.get("custom_main_prompt") or "").strip() or DEFAULT_MAIN_PROMPT_TEMPLATE.strip()
    )
    current_directives = (
        (prompts_cfg.get("custom_prompt_directives") or "").strip() or DEFAULT_DIRECTIVES.strip()
    )

    with st.expander("🛠️ KI-Prompt-Konfiguration", expanded=False):
        st.caption("Systemprompt und redaktionelle Richtlinien für das KI-Briefing anpassen.")

        ki_main_prompt = st.text_area(
            "Haupt-Prompt (Rolle, Struktur & Format):",
            value=current_main_prompt,
            key="input_ki_main_prompt",
            help="Definiert die Rollen- und Strukturvorgaben für das KI-Briefing (Top 5 Links). Die Platzhalter {lang_name}, {active_directives} und {context_data} werden automatisch eingesetzt.",
            height=200,
            disabled=not is_admin,
        )

        ki_prompt_directives = st.text_area(
            "Redaktionelle Filter & Direktiven (Erweiterte Regeln):",
            value=current_directives,
            key="input_ki_prompt_directives",
            help="Hier kannst du z. B. vorgeben: 'Filtere reine Werbung und Sonderangebote heraus. Ignoriere Krypto. Fokussiere auf Berliner Lokalthemen.'",
            height=100,
            disabled=not is_admin,
        )

        if is_admin:
            col_save_p, col_reset_p = st.columns([1, 1])
            with col_save_p:
                save_prompts_clicked = st.button(
                    "💾 Prompts in prompts.yaml speichern",
                    key="btn_save_ki_prompts",
                    use_container_width=True,
                )
            with col_reset_p:
                reset_prompts_clicked = st.button(
                    "↩️ Standard-Hauptprompt laden",
                    key="btn_reset_ki_main_prompt",
                    use_container_width=True,
                )

            if save_prompts_clicked:
                with st.spinner("Speichere Prompts in prompts.yaml..."):
                    save_prompts({
                        "custom_main_prompt": ki_main_prompt.strip(),
                        "custom_prompt_directives": ki_prompt_directives.strip(),
                    })
                    sync_sources_to_github(
                        commit_message="chore(prompt): update custom AI prompts via dashboard",
                    )
                    set_dismissible_notice(
                        "ki_notice", "Prompts erfolgreich in prompts.yaml gespeichert!", icon="✨"
                    )
                    st.rerun()

            if reset_prompts_clicked:
                with st.spinner("Setze Hauptprompt auf Standard zurück..."):
                    save_prompts({
                        "custom_main_prompt": DEFAULT_MAIN_PROMPT_TEMPLATE.strip(),
                        "custom_prompt_directives": ki_prompt_directives.strip(),
                    })
                    st.session_state["input_ki_main_prompt"] = DEFAULT_MAIN_PROMPT_TEMPLATE.strip()
                    set_dismissible_notice("ki_notice", "Standard-Prompt wiederhergestellt!", icon="✨")
                    st.rerun()

    # Direkt unterhalb der 2 Expander: Generieren-Button (nur Admin)
    if is_admin:
        is_generating = bool(st.session_state.get("is_generating_briefing", False))
        btn_generate = st.button(
            "✨ Neues Briefing generieren",
            type="primary",
            use_container_width=True,
            key="btn_generate_briefing",
            disabled=is_generating or not ai_brain_online,
            help=None if ai_brain_online else "AI_BRAIN_KEY erforderlich, um ein Briefing über AI-Brain zu generieren.",
        )

        if btn_generate:
            total_items = sum(len(v) for v in news_data.values())
            if total_items == 0:
                st.warning("Keine aktuellen Artikel für das Briefing vorhanden.")
            else:
                st.session_state["is_generating_briefing"] = True
                with st.spinner("Generiere KI-Briefing über AI-Brain (tier=auto)..."):
                    t_start = time.perf_counter()
                    try:
                        briefing_res = summarize_news_with_gemini(
                            news_data,
                            main_prompt_template=ki_main_prompt,
                            custom_directives=ki_prompt_directives,
                            return_result=True,
                        )
                        summary = briefing_res.markdown
                        used_model_label = briefing_res.display_label
                        duration = time.perf_counter() - t_start
                        logger.info(
                            "Generated AI briefing (%d chars in %.2fs) using %s.",
                            len(summary),
                            duration,
                            used_model_label,
                        )

                        # Save to session and storage
                        st.session_state["cached_summary"] = summary
                        st.session_state["cached_summary_model"] = used_model_label
                        current_briefing = summary
                        current_model_label = used_model_label

                        storage = get_storage()
                        briefing_date = time.strftime("%Y-%m-%d")
                        storage.save_briefing(briefing_date, summary, used_model_label)

                        # Update RSS
                        try:
                            export_briefing_rss(summary, news_data=news_data)
                        except Exception as e_rss:  # noqa: BLE001 - RSS-Fehler darf Briefing nicht verwerfen
                            logger.warning("Could not export briefing RSS: %s", e_rss)

                        st.session_state["is_generating_briefing"] = False
                        set_dismissible_notice(
                            "ki_notice",
                            f"KI-Briefing erfolgreich generiert ({used_model_label})!",
                            icon="✨",
                        )
                        st.rerun()
                    except BrainError as exc:
                        st.session_state["is_generating_briefing"] = False
                        logger.error("AI-Brain briefing generation failed: %s", exc)
                        st.error(f"AI-Brain Fehler bei der Generierung: {exc}")
                    except Exception as exc:  # noqa: BLE001 - UI-Fehleranzeige für unerwartete Ausnahmen
                        st.session_state["is_generating_briefing"] = False
                        logger.error("AI briefing generation failed: %s", exc)
                        st.error(f"Fehler bei der Generierung: {exc}")

    # Display briefing content
    if current_briefing:
        st.markdown("---")
        if current_model_label:
            st.caption(f"🤖 Generiert mit: **{current_model_label}**")
        st.markdown(current_briefing, unsafe_allow_html=True)


