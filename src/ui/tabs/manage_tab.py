"""
Quellen & Feeds verwalten tab module for News Bot UI.
Provides management of categories, feeds, global settings, and durable Cloud-DB + GitHub configuration persistence.
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import logging
from typing import Any

import streamlit as st
import yaml

from src.aggregator import (
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_MAX_ARTICLE_AGE_HOURS,
    PROMPT_KEYS,
    add_category,
    add_feed,
    delete_category,
    delete_feed,
    fetch_feed_items,
    get_offline_enabled_feeds,
    load_prompts,
    load_sources,
    normalize_keywords,
    rename_category,
    save_sources,
    test_feed_connection,
    update_feed,
    validate_feed_url,
)
from src.offline_archiver import start_offline_archive_task
from src.storage import get_storage, is_cloud_storage
from src.ui.background import run_in_background
from src.ui.state import get_news_data
from src.ui.styles import embed_client_script, render_dismissible_notice, set_dismissible_notice

logger = logging.getLogger(__name__)

_FORM_KEY_PREFIXES: tuple[str, ...] = (
    "ed_",
    "inp_set_",
    "inp_new_",
    "sel_ren_cat",
    "sel_new_feed_cat",
)

_EDIT_STATE_KEYS: tuple[str, ...] = (
    "editing_feed_key",
    "editing_feed_url",
    "editing_feed_name",
    "editing_feed_category",
    "last_edited_category",
    "last_saved_feed_key",
)


def _feed_widget_hash(cat_name: str, feed_url: str) -> str:
    """Erzeugt einen stabilen, kollisionsfreien Widget-Key-Hash pro (Kategorie, Feed-URL)."""
    raw = f"{cat_name.strip().lower()}|{feed_url.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _parse_ad_patterns(raw_text: str) -> list[str]:
    """Bereinigt und dedupliziert zeilenweise eingegebene Ad-Filter-Patterns."""
    seen: set[str] = set()
    parsed: list[str] = []
    for line in (raw_text or "").splitlines():
        pat = line.strip()
        if pat and pat not in seen:
            seen.add(pat)
            parsed.append(pat)
    return parsed


def _clear_manage_form_keys() -> None:
    """Entfernt temporäre Formular- und Editier-Keys aus dem Session State."""
    keys_to_clear = [
        k for k in list(st.session_state.keys())
        if k.startswith(_FORM_KEY_PREFIXES)
    ]
    for key in keys_to_clear:
        del st.session_state[key]
    for state_key in _EDIT_STATE_KEYS:
        st.session_state.pop(state_key, None)


def _has_pending_settings_edits(working_config: dict[str, Any]) -> bool:
    """Prüft, ob in den globalen Einstellungs-Widgets unübernommene Änderungen vorliegen."""
    settings = working_config.get("settings", {})
    if not isinstance(settings, dict):
        return False

    if "inp_set_lang" in st.session_state:
        if str(st.session_state["inp_set_lang"]).strip() != str(settings.get("language", "de")).strip():
            return True
    if "inp_set_retention_days" in st.session_state:
        curr_ret = int(settings.get("archive_retention_days", DEFAULT_ARCHIVE_RETENTION_DAYS))
        if int(st.session_state["inp_set_retention_days"]) != curr_ret:
            return True
    if "inp_set_style" in st.session_state:
        if str(st.session_state["inp_set_style"]).strip() != str(settings.get("summary_style", "tldr")).strip():
            return True
    if "inp_set_filter_ads" in st.session_state:
        if bool(st.session_state["inp_set_filter_ads"]) != bool(settings.get("filter_ads", True)):
            return True
    if "inp_set_ad_patterns" in st.session_state:
        curr_pat = settings.get("ad_patterns", [])
        expected = [str(p).strip() for p in curr_pat if str(p).strip()] if isinstance(curr_pat, list) else _parse_ad_patterns(str(curr_pat or ""))
        if _parse_ad_patterns(str(st.session_state["inp_set_ad_patterns"])) != expected:
            return True
    return False


def _apply_pending_settings_from_state(working_config: dict[str, Any]) -> None:
    """Übernimmt noch nicht explizit bestätigte globale Einstellungs-Widgets vor dem Speichern."""
    settings = working_config.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        working_config["settings"] = settings

    if "inp_set_lang" in st.session_state:
        settings["language"] = str(st.session_state["inp_set_lang"]).strip() or "de"
    if "inp_set_retention_days" in st.session_state:
        settings["archive_retention_days"] = int(st.session_state["inp_set_retention_days"])
    if "inp_set_style" in st.session_state:
        settings["summary_style"] = str(st.session_state["inp_set_style"]).strip() or "tldr"
    if "inp_set_filter_ads" in st.session_state:
        settings["filter_ads"] = bool(st.session_state["inp_set_filter_ads"])
    if "inp_set_ad_patterns" in st.session_state:
        settings["ad_patterns"] = _parse_ad_patterns(str(st.session_state["inp_set_ad_patterns"]))

    for deprecated_key in (
        "strip_ads",
        "ad_keywords",
        "max_article_age_weeks",
        "max_age_weeks",
        "batch_sync_interval_minutes",
        "streamlit_app_url",
    ):
        settings.pop(deprecated_key, None)


def _has_pending_feed_edits(working_config: dict[str, Any]) -> bool:
    """Prüft, ob in aufgeklappten Feed-Bearbeitungsfeldern unübernommene Änderungen vorliegen."""
    for cat in working_config.get("categories", []):
        cat_name = str(cat.get("name") or "Allgemein").strip()
        for feed in cat.get("feeds", []):
            f_url = str(feed.get("url") or "").strip()
            f_name = str(feed.get("name") or "Unbenannt").strip()
            key_hash = _feed_widget_hash(cat_name, f_url)

            if f"ed_name_{key_hash}" in st.session_state:
                if str(st.session_state[f"ed_name_{key_hash}"]).strip() != f_name:
                    return True
            if f"ed_url_{key_hash}" in st.session_state:
                if str(st.session_state[f"ed_url_{key_hash}"]).strip() != f_url:
                    return True
            if f"ed_cat_{key_hash}" in st.session_state:
                if str(st.session_state[f"ed_cat_{key_hash}"]).strip().lower() != cat_name.lower():
                    return True
            if f"ed_inc_{key_hash}" in st.session_state:
                if normalize_keywords(st.session_state[f"ed_inc_{key_hash}"]) != normalize_keywords(feed.get("include_keywords")):
                    return True
            if f"ed_exc_{key_hash}" in st.session_state:
                if normalize_keywords(st.session_state[f"ed_exc_{key_hash}"]) != normalize_keywords(feed.get("exclude_keywords")):
                    return True
            if f"ed_offline_{key_hash}" in st.session_state:
                if bool(st.session_state[f"ed_offline_{key_hash}"]) != bool(feed.get("offline_download", False)):
                    return True
    return False


def _apply_pending_feed_edits_from_state(working_config: dict[str, Any]) -> list[str]:
    """Übernimmt noch nicht explizit bestätigte Inline-Feed-Änderungen inkl. SSRF-Validierung vor dem Speichern."""
    errors: list[str] = []
    pending_updates: list[tuple[str, str, str, str, str, str, str, bool]] = []

    for cat in list(working_config.get("categories", [])):
        cat_name = str(cat.get("name") or "Allgemein").strip()
        for feed in list(cat.get("feeds", [])):
            f_url = str(feed.get("url") or "").strip()
            f_name = str(feed.get("name") or "Unbenannt").strip()
            f_inc = normalize_keywords(feed.get("include_keywords"))
            f_exc = normalize_keywords(feed.get("exclude_keywords"))
            f_offline = bool(feed.get("offline_download", False))
            key_hash = _feed_widget_hash(cat_name, f_url)

            has_any_widget = any(
                f"{prefix}{key_hash}" in st.session_state
                for prefix in ("ed_name_", "ed_url_", "ed_cat_", "ed_inc_", "ed_exc_", "ed_offline_")
            )
            if not has_any_widget:
                continue

            new_name = str(st.session_state.get(f"ed_name_{key_hash}", f_name)).strip()
            new_url = str(st.session_state.get(f"ed_url_{key_hash}", f_url)).strip()
            new_cat = str(st.session_state.get(f"ed_cat_{key_hash}", cat_name)).strip() or cat_name
            new_inc_raw = str(st.session_state.get(f"ed_inc_{key_hash}", ", ".join(f_inc)))
            new_exc_raw = str(st.session_state.get(f"ed_exc_{key_hash}", ", ".join(f_exc)))
            new_offline = bool(st.session_state.get(f"ed_offline_{key_hash}", f_offline))

            changed = (
                new_name != f_name
                or new_url != f_url
                or new_cat.lower() != cat_name.lower()
                or normalize_keywords(new_inc_raw) != f_inc
                or normalize_keywords(new_exc_raw) != f_exc
                or new_offline != f_offline
            )
            if not changed:
                continue
            if not new_name or not new_url:
                errors.append(f"Feed '{f_name}': Name und URL dürfen nicht leer sein.")
                continue
            url_ok, url_err = validate_feed_url(new_url, resolve_dns=False)
            if not url_ok:
                errors.append(f"Feed '{new_name}': {url_err or 'Ungültige Feed-URL.'}")
                continue
            pending_updates.append((cat_name, f_url, new_name, new_url, new_cat, new_inc_raw, new_exc_raw, new_offline))

    if errors:
        return errors

    for cat_name, old_url, new_name, new_url, new_cat, new_inc_raw, new_exc_raw, new_offline in pending_updates:
        update_feed(
            cat_name,
            old_url,
            new_name=new_name,
            new_url=new_url,
            new_category=new_cat,
            include_keywords=new_inc_raw,
            exclude_keywords=new_exc_raw,
            offline_download=new_offline,
            config=working_config,
            save_to_disk=False,
        )
    return []


def _get_storage_status_caption() -> str:
    """Liefert eine kompakte, ehrliche Statuszeile zum aktiven Datenbank-Backend."""
    try:
        storage = get_storage()
        if is_cloud_storage(storage):
            return "☁️ Speicher: Turso Cloud-DB aktiv"
        return "⚠️ Speicher: Nur lokale SQLite-DB aktiv (nicht dauerhaft in der Cloud)"
    except Exception as exc:  # noqa: BLE001 - Anzeige darf UI nicht abbrechen
        logger.debug("Speicher-Status konnte nicht ermittelt werden: %s", exc)
        return "⚠️ Speicher-Backend konnte nicht geprüft werden"


def _preserve_latest_prompts(working_config: dict[str, Any]) -> None:
    """Stellt sicher, dass im KI-Tab gespeicherte Prompts beim Sichern im Verwalten-Tab erhalten bleiben."""
    try:
        latest_prompts = load_prompts()
    except Exception as exc:  # noqa: BLE001 - Fallback auf bestehende Prompts im Entwurf
        logger.debug("Aktuelle prompts.yaml konnte vor dem Speichern nicht gelesen werden: %s", exc)
        return
    if not isinstance(latest_prompts, dict):
        return
    settings = working_config.setdefault("settings", {})
    for key in PROMPT_KEYS:
        val = latest_prompts.get(key)
        if isinstance(val, str) and val.strip():
            settings[key] = val


def _extract_feed_index(config: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Erstellt ein Mapping {feed_url: (category_name, feed_dict)} für Konfigurations-Diffs."""
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    for cat in config.get("categories", []):
        cat_name = str(cat.get("name") or "").strip()
        if not cat_name:
            continue
        for feed in cat.get("feeds", []):
            f_url = str(feed.get("url") or "").strip()
            if f_url:
                index[f_url] = (cat_name, feed)
    return index


def _sync_news_cache_after_save(
    previous_config: dict[str, Any],
    saved_config: dict[str, Any],
) -> int:
    """
    Aktualisiert Artikel-Cache & DB unmittelbar nach dem Speichern:
    - Entfernt Artikel gelöschter Feeds/Kategorien sofort aus Session-Cache und aktiver DB-Tabelle.
    - Lädt Artikel neu hinzugefügter Feeds gezielt nach, sodass Artikel-Tab & KI-Briefing sofort aktuell sind.
    Gibt die Anzahl neu geladener Artikel zurück.
    """
    prev_feeds = _extract_feed_index(previous_config)
    new_feeds = _extract_feed_index(saved_config)

    removed_urls = set(prev_feeds.keys()) - set(new_feeds.keys())
    added_urls = set(new_feeds.keys()) - set(prev_feeds.keys())
    valid_categories = {
        str(c.get("name") or "").strip()
        for c in saved_config.get("categories", [])
        if str(c.get("name") or "").strip()
    }
    removed_pairs = {
        (prev_feeds[u][0], str(prev_feeds[u][1].get("name") or "").strip())
        for u in removed_urls
    }

    # 1. Gelöschte Feeds & Kategorien sofort aus dem Session-Cache entfernen
    cached_news = st.session_state.get("cached_news_data")
    if isinstance(cached_news, dict) and (removed_urls or valid_categories):
        cleaned_news: dict[str, list[dict[str, Any]]] = {}
        for cat_name, items in cached_news.items():
            if valid_categories and cat_name not in valid_categories:
                continue
            kept_items = [
                art for art in items
                if str(art.get("source_url") or "").strip() not in removed_urls
                and (cat_name, str(art.get("source") or "").strip()) not in removed_pairs
            ]
            if kept_items:
                cleaned_news[cat_name] = kept_items
        st.session_state["cached_news_data"] = cleaned_news

    # 2. Gelöschte Feed-Artikel im Hintergrund auch aus der aktiven DB-Tabelle entfernen
    if removed_urls:
        removed_list = list(removed_urls)

        def _prune_deleted_articles() -> None:
            try:
                get_storage().delete_articles_by_source_urls(removed_list)
            except Exception as exc:  # noqa: BLE001 - Hintergrund-Bereinigung darf UI nicht stören
                logger.warning("Konnte Artikel gelöschter Feeds nicht aus DB bereinigen: %s", exc)

        run_in_background(_prune_deleted_articles)

    # 3. Caches invalidieren
    get_news_data.clear()
    st.session_state.pop("cached_rss_registry", None)
    st.session_state.pop("_cached_effective_update_ts", None)

    # 4. Neu hinzugefügte Feeds gezielt & parallel nachladen (max. 5 gleichzeitig, schnell)
    if not added_urls:
        return 0

    settings = saved_config.get("settings", {})
    filter_ads = bool(settings.get("filter_ads", True))
    ad_patterns = settings.get("ad_patterns", [])
    added_tasks = [(new_feeds[u][0], new_feeds[u][1]) for u in added_urls]

    def _fetch_added(task: tuple[str, dict[str, Any]]) -> tuple[str, str, str, list[dict[str, Any]]]:
        c_name, f_dict = task
        f_url = str(f_dict.get("url") or "").strip()
        f_name = str(f_dict.get("name") or f_url).strip()
        items = fetch_feed_items(
            f_url,
            include_keywords=f_dict.get("include_keywords"),
            exclude_keywords=f_dict.get("exclude_keywords"),
            filter_ads=filter_ads,
            ad_patterns=ad_patterns,
            max_age_hours=DEFAULT_MAX_ARTICLE_AGE_HOURS,
        )
        return c_name, f_name, f_url, items

    fetched_articles: list[dict[str, Any]] = []
    try:
        max_workers = min(4, len(added_tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for c_name, f_name, f_url, items in executor.map(_fetch_added, added_tasks):
                if not items:
                    continue
                target_news = st.session_state.setdefault("cached_news_data", {})
                cat_list = target_news.setdefault(c_name, [])
                existing_links = {str(a.get("link") or "").strip() for a in cat_list}
                for art in items:
                    art["source"] = f_name
                    art["source_url"] = f_url
                    art["category"] = c_name
                    link = str(art.get("link") or "").strip()
                    if link and link not in existing_links:
                        existing_links.add(link)
                        cat_list.append(art)
                        fetched_articles.append(art)
                cat_list.sort(key=lambda x: x.get("timestamp", 0.0), reverse=True)
    except Exception as exc:  # noqa: BLE001 - Fehler beim gezielten Vorabladen darf Speichern nicht abbrechen
        logger.warning("Gezieltes Nachladen neuer Feeds fehlgeschlagen: %s", exc)

    if fetched_articles:
        articles_to_persist = list(fetched_articles)

        def _persist_new_articles() -> None:
            try:
                get_storage().save_articles(articles_to_persist)
            except Exception as exc:  # noqa: BLE001 - Hintergrund-Speichern darf UI nicht stören
                logger.warning("Konnte Artikel neuer Feeds nicht in DB speichern: %s", exc)

        run_in_background(_persist_new_articles)

    return len(fetched_articles)


def _build_save_notice(save_res: Any, newly_fetched_count: int = 0) -> tuple[str, str, str]:
    """
    Formatiert eine strikt ehrliche Statusmeldung `(level, icon, message)` für den Speichervorgang.
    Ein grünes `success` (`✅`) wird AUSSCHLIESSLICH zurückgegeben, wenn sowohl die echte Cloud-DB
    (Turso, inkl. Read-Back-Verifikation) als auch GitHub erfolgreich synchronisiert wurden.
    """
    extra = f" · {newly_fetched_count} neue Artikel geladen" if newly_fetched_count > 0 else ""

    if not isinstance(save_res, dict):
        return "success", "✅", f"Änderungen dauerhaft gespeichert & synchronisiert{extra}!"

    db_saved = bool(save_res.get("db_saved", False))
    db_verified = bool(save_res.get("db_verified", db_saved))
    storage_backend = str(save_res.get("storage_backend") or "").strip().lower()
    if "is_cloud_db" in save_res:
        is_cloud_db = bool(save_res["is_cloud_db"])
    else:
        is_cloud_db = db_saved and storage_backend != "sqlite"

    cloud_db_ok = db_saved and db_verified and is_cloud_db
    gh_synced = bool(save_res.get("success", False))
    gh_err = str(save_res.get("error") or "").strip()
    db_err = str(save_res.get("db_error") or "").strip()

    if cloud_db_ok and gh_synced:
        return "success", "✅", f"Änderungen dauerhaft in Cloud-Datenbank & GitHub gespeichert{extra}!"
    if cloud_db_ok and not gh_synced:
        hint = gh_err or "GitHub-Sync nicht konfiguriert"
        return (
            "warning",
            "⚠️",
            (
                f"In Turso Cloud-Datenbank gespeichert & verifiziert{extra} – "
                f"aber GitHub-Sync fehlgeschlagen ({hint}). "
                "Änderungen bleiben über die Cloud-DB erhalten und werden beim nächsten Daily-Run zu GitHub übertragen."
            ),
        )
    if not cloud_db_ok and gh_synced:
        db_reason = db_err or (
            "nur lokale SQLite aktiv" if db_saved and not is_cloud_db else "Cloud-Datenbank nicht erreichbar"
        )
        return (
            "warning",
            "⚠️",
            f"Auf GitHub gespeichert{extra}, aber Cloud-Datenbank-Sync unvollständig ({db_reason}).",
        )

    db_reason = db_err or (
        "nur temporäre lokale SQLite-DB aktiv – nicht persistent auf Streamlit Cloud"
        if db_saved and not is_cloud_db
        else "Speichern fehlgeschlagen"
    )
    return (
        "error",
        "❌",
        f"NICHT dauerhaft gespeichert! Cloud-DB: {db_reason} · GitHub: {gh_err or 'fehlgeschlagen'}.",
    )


def render_feed_test_result(t_res: dict[str, Any]) -> None:
    """Renders a clean expandable infobox with stream test diagnostics."""
    if not t_res:
        return

    success = t_res.get("success", False)
    item_count = t_res.get("item_count", 0)
    title = t_res.get("title", "Unbekannter Titel")
    stream_type = t_res.get("stream_type", "Unbekannt")
    status_code = t_res.get("status_code", "-")
    latency_ms = t_res.get("latency_ms", 0)
    content_type = t_res.get("content_type", "")
    content_len = t_res.get("content_length", 0)
    err = t_res.get("error")

    icon = "✅" if (success and item_count > 0) else ("⚠️" if success else "❌")
    box_header = (
        f"{icon} Stream-Test: {title} ({stream_type} • {item_count} Artikel)"
        if (success and item_count > 0)
        else f"{icon} Stream-Test: {title} ({'0 Artikel' if success else err or 'Fehlgeschlagen'})"
    )

    with st.expander(box_header, expanded=True):
        if success and item_count > 0:
            st.success(f"**Stream erfolgreich verifiziert:** **{title}** ({stream_type}) — **{item_count} Einträge** gefunden!")
        elif success and item_count == 0:
            st.warning(f"**Stream erreichbar, aber leer:** **{title}** ({stream_type}) lieferte aktuell **0 Artikel**.")
        else:
            st.error(f"**Stream-Test fehlgeschlagen:** {err or 'Unbekannter Fehler'}")

        col_info1, col_info2 = st.columns(2)
        with col_info1:
            st.markdown(f"• **Stream-Format:** `{stream_type}`")
            st.markdown(f"• **Gefundene Artikel:** **{item_count}** Einträge")
            if t_res.get("site_url"):
                st.markdown(f"• **Website der Quelle:** [{t_res['site_url']}]({t_res['site_url']})")
        with col_info2:
            status_badge = f"`HTTP {status_code}`" if status_code else "`-`"
            st.markdown(f"• **Server-Status:** {status_badge} ({latency_ms} ms)")
            ct_clean = content_type.split(";")[0].strip() if content_type else "unbekannt"
            size_str = f" • {content_len / 1024:.1f} KB" if content_len else ""
            st.markdown(f"• **Content-Type:** `{ct_clean}`{size_str}")


def render_manage_tab(
    working_config: dict[str, Any],
    is_admin: bool,
) -> None:
    """Renders the settings and feed management tab."""
    st.subheader("⚙️ Quellen & Feeds verwalten")
    st.caption("Kategorien, Feeds und Einstellungen verwalten.")

    if not is_admin:
        st.info("🔒 Dieser Bereich erfordert eine Administrator-Anmeldung. Bitte melde dich links in der Leiste an.")
        return

    # Prominenter Erfolgs- / Warn- / Fehler-Hinweis nach Aktionen
    if st.session_state.get("manage_notice"):
        render_dismissible_notice(st.session_state["manage_notice"], notice_key="manage_notice", icon="✅")

    has_unsaved_changes = (
        bool(st.session_state.get("has_unsaved_changes", False))
        or _has_pending_settings_edits(working_config)
        or _has_pending_feed_edits(working_config)
    )

    # Top Status & Save Bar
    with st.container(border=True):
        col_st1, col_st2, col_st3 = st.columns([3, 1, 1], vertical_alignment="center")
        with col_st1:
            if has_unsaved_changes:
                st.warning("⚠️ **Ungespeicherte Änderungen.**")
            else:
                st.success("✅ **Alle Feeds & Einstellungen sind aktuell.**")
            st.caption(_get_storage_status_caption())
        with col_st2:
            btn_save_label = "💾 Jetzt sichern" if has_unsaved_changes else "💾 Gespeichert"
            btn_save_disabled = not has_unsaved_changes or bool(st.session_state.get("is_saving_sources", False))
            if st.button(
                btn_save_label,
                type="primary" if has_unsaved_changes else "secondary",
                use_container_width=True,
                key="top_save_sources_btn",
                disabled=btn_save_disabled,
                help="Änderungen sichern" if has_unsaved_changes else "Alles aktuell.",
            ):
                st.session_state["is_saving_sources"] = True
                try:
                    previous_config = copy.deepcopy(
                        st.session_state.get("last_loaded_saved_config") or working_config
                    )
                    feed_edit_errors = _apply_pending_feed_edits_from_state(working_config)
                    if feed_edit_errors:
                        set_dismissible_notice(
                            "manage_notice",
                            "Speichern abgebrochen: " + " | ".join(feed_edit_errors),
                            icon="❌",
                            level="error",
                        )
                    else:
                        _apply_pending_settings_from_state(working_config)
                        _preserve_latest_prompts(working_config)
                        if "settings" in working_config and isinstance(working_config["settings"], dict):
                            working_config["settings"].pop("ad_keywords", None)

                        save_res = save_sources(
                            working_config,
                            sync_github=True,
                            persist_db=True,
                            include_rss_feeds=False,
                        )
                        if get_offline_enabled_feeds(working_config):
                            start_offline_archive_task(config=working_config)

                        reloaded_cfg = load_sources()
                        newly_fetched = _sync_news_cache_after_save(previous_config, reloaded_cfg)

                        notice_level, notice_icon, notice_msg = _build_save_notice(
                            save_res,
                            newly_fetched_count=newly_fetched,
                        )
                        st.session_state["working_sources_config"] = copy.deepcopy(reloaded_cfg)
                        st.session_state["last_loaded_saved_config"] = copy.deepcopy(reloaded_cfg)
                        # Bei komplett fehlgeschlagener Persistenz Button aktiv lassen, damit Retry direkt möglich ist
                        st.session_state["has_unsaved_changes"] = notice_level == "error"
                        _clear_manage_form_keys()

                        set_dismissible_notice(
                            "manage_notice",
                            notice_msg,
                            icon=notice_icon,
                            level=notice_level,
                        )
                finally:
                    st.session_state["is_saving_sources"] = False
                st.rerun()
        with col_st3:
            if has_unsaved_changes:
                if st.button("↩️ Verwerfen", use_container_width=True, key="top_discard_sources_btn"):
                    with st.spinner("Verwerfe ungespeicherte Änderungen..."):
                        reloaded_cfg = load_sources()
                        st.session_state["working_sources_config"] = copy.deepcopy(reloaded_cfg)
                        st.session_state["last_loaded_saved_config"] = copy.deepcopy(reloaded_cfg)
                        st.session_state["has_unsaved_changes"] = False
                        _clear_manage_form_keys()
                        set_dismissible_notice(
                            "manage_notice",
                            "Ungespeicherte Änderungen verworfen.",
                            icon="✅",
                            level="success",
                        )
                        embed_client_script("setTimeout(function(){ window.location.reload(); }, 60);")

    # --- Sektion 1: Kategorien verwalten (standardmäßig zugeklappt) ---
    with st.expander("📁 Kategorien verwalten", expanded=False):
        if st.session_state.get("manage_cat_notice"):
            render_dismissible_notice(st.session_state["manage_cat_notice"], notice_key="manage_cat_notice", icon="📁")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            new_cat_name = st.text_input("Neue Kategorie:", placeholder="z. B. Wirtschaft & Finanzen", key="inp_new_cat_name")
            if st.button("➕ Kategorie hinzufügen", key="btn_add_cat", use_container_width=True):
                if new_cat_name.strip():
                    with st.spinner("Füge Kategorie hinzu..."):
                        add_category(new_cat_name.strip(), config=working_config, save_to_disk=False)
                        st.session_state["has_unsaved_changes"] = True
                        st.session_state.pop("inp_new_cat_name", None)
                        set_dismissible_notice("manage_cat_notice", f"Kategorie '{new_cat_name.strip()}' hinzugefügt.", icon="📁")
                        set_dismissible_notice("manage_notice", f"Kategorie '{new_cat_name.strip()}' hinzugefügt.", icon="✅")
                        st.rerun()
                else:
                    st.error("Bitte einen Kategorienamen angeben.")

        with col_c2:
            cat_names = [c.get("name", "").strip() for c in working_config.get("categories", []) if c.get("name")]
            if cat_names:
                col_ren1, col_ren2 = st.columns([1, 1])
                with col_ren1:
                    cat_to_rename = st.selectbox("Kategorie umbenennen:", options=cat_names, key="sel_ren_cat")
                with col_ren2:
                    cat_new_name = st.text_input("Neuer Name:", placeholder="Neuer Name...", key="inp_ren_cat_name")
                if st.button("✏️ Umbenennen", key="btn_ren_cat", use_container_width=True):
                    if cat_new_name.strip() and cat_to_rename:
                        try:
                            with st.spinner("Benenne Kategorie um..."):
                                rename_category(cat_to_rename, cat_new_name.strip(), config=working_config, save_to_disk=False)
                                st.session_state["has_unsaved_changes"] = True
                                st.session_state.pop("inp_ren_cat_name", None)
                                set_dismissible_notice("manage_cat_notice", f"Kategorie umbenannt in '{cat_new_name.strip()}'.", icon="📁")
                                set_dismissible_notice("manage_notice", f"Kategorie umbenannt in '{cat_new_name.strip()}'.", icon="✅")
                                st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

    # --- Sektion 2: Neuen RSS-Feed hinzufügen (standardmäßig zugeklappt) ---
    with st.expander("➕ Neuen RSS-Feed hinzufügen", expanded=False):
        if st.session_state.get("manage_new_feed_notice"):
            render_dismissible_notice(st.session_state["manage_new_feed_notice"], notice_key="manage_new_feed_notice", icon="➕")
        col_f1, col_f2 = st.columns([1, 1])
        with col_f1:
            feed_url_input = st.text_input("Feed-URL:", placeholder="https://example.com/feed.xml", key="inp_new_feed_url")
            feed_name_input = st.text_input("Feed-Name:", placeholder="z. B. TechCrunch News", key="inp_new_feed_name")
        with col_f2:
            target_cat = st.selectbox("Ziel-Kategorie:", options=cat_names if cat_names else ["Allgemein"], key="sel_new_feed_cat")
            col_kw1, col_kw2 = st.columns(2)
            with col_kw1:
                inc_kw_input = st.text_input("🟢 Nur mit Keywords (Komma):", placeholder="z. B. KI, Python", key="inp_new_feed_inc")
            with col_kw2:
                exc_kw_input = st.text_input("🔴 Ohne Keywords (Komma):", placeholder="z. B. Krypto, Sport", key="inp_new_feed_exc")

        offline_dl_input = st.checkbox(
            "📥 Offline-Download aktivieren (Webseite, Text & Bilder für 3 Monate im Archiv speichern)",
            value=False,
            key="inp_new_feed_offline",
            help="Wenn aktiviert, werden alle Artikel dieses Feeds automatisch im Hintergrund als Offline-HTML-Abbild (mit Text & Bildern) für 3 Monate in der Datenbank archiviert.",
        )

        col_tst, col_add = st.columns([1, 1])
        with col_tst:
            if st.button("🔍 Feed testen", key="btn_test_new_feed", use_container_width=True):
                if feed_url_input.strip():
                    with st.spinner("Prüfe Feed-Verbindung..."):
                        t_res = test_feed_connection(feed_url_input.strip())
                        render_feed_test_result(t_res)
                else:
                    st.error("Bitte eine Feed-URL eingeben.")
        with col_add:
            if st.button("➕ Feed hinzufügen", type="primary", key="btn_submit_new_feed", use_container_width=True):
                clean_url = feed_url_input.strip()
                clean_name = feed_name_input.strip()
                if clean_url and clean_name:
                    url_ok, url_err = validate_feed_url(clean_url, resolve_dns=False)
                    if not url_ok:
                        st.error(url_err or "Ungültige Feed-URL.")
                    else:
                        with st.spinner("Füge Feed hinzu..."):
                            add_feed(
                                category_name=target_cat,
                                feed_name=clean_name,
                                feed_url=clean_url,
                                include_keywords=inc_kw_input.strip(),
                                exclude_keywords=exc_kw_input.strip(),
                                offline_download=bool(offline_dl_input),
                                config=working_config,
                                save_to_disk=False,
                            )
                            if offline_dl_input:
                                start_offline_archive_task(
                                    config=working_config,
                                    target_feed_url=clean_url,
                                )
                            st.session_state["has_unsaved_changes"] = True
                            for k in (
                                "inp_new_feed_url",
                                "inp_new_feed_name",
                                "inp_new_feed_inc",
                                "inp_new_feed_exc",
                                "inp_new_feed_offline",
                            ):
                                st.session_state.pop(k, None)
                            set_dismissible_notice("manage_notice", f"Feed '{clean_name}' hinzugefügt!", icon="✅")
                            st.rerun()
                else:
                    st.error("Bitte Feed-URL und Feed-Name ausfüllen.")

    st.markdown("---")

    # --- Sektion 3: Aktive Feeds nach Kategorien bearbeiten & löschen ---
    st.markdown("### 📋 Aktive Feeds nach Kategorien")
    categories = sorted(working_config.get("categories", []), key=lambda c: c.get("name", "").strip().lower())

    for cat_idx, cat in enumerate(categories):
        cat_name = cat.get("name", "Allgemein")
        feeds = sorted(cat.get("feeds", []), key=lambda f: f.get("name", "").strip().lower())
        cat_hash = hashlib.sha256(cat_name.strip().lower().encode("utf-8")).hexdigest()[:8]

        is_cat_expanded = (
            st.session_state.get("last_edited_category") == cat_name
            or st.session_state.get("editing_feed_category") == cat_name
        )

        with st.expander(f"📁 **{cat_name}** ({len(feeds)} Feeds)", expanded=is_cat_expanded):
            col_cinf, col_cdel = st.columns([5, 1], vertical_alignment="center")
            with col_cinf:
                st.caption(f"{len(feeds)} konfigurierte Feeds")
            with col_cdel:
                with st.popover("🗑️ Kategorie löschen", use_container_width=True):
                    st.markdown(f"Kategorie **'{cat_name}'** samt aller Feeds wirklich löschen?")
                    if st.button("Kategorie löschen", key=f"del_cat_{cat_hash}_{cat_idx}", type="primary", use_container_width=True):
                        delete_category(cat_name, config=working_config, save_to_disk=False)
                        st.session_state["has_unsaved_changes"] = True
                        set_dismissible_notice("manage_notice", f"Kategorie '{cat_name}' gelöscht.", icon="✅")
                        st.rerun()

            for feed in feeds:
                f_name = feed.get("name", "Unbenannt")
                f_url = feed.get("url", "")
                f_inc = feed.get("include_keywords", [])
                f_exc = feed.get("exclude_keywords", [])
                f_offline = bool(feed.get("offline_download", False))
                key_hash = _feed_widget_hash(cat_name, f_url)

                with st.container(border=True):
                    col_top1, col_top2 = st.columns([5, 1], vertical_alignment="center")
                    with col_top1:
                        offline_badge = " · `📥 Offline-Archiv aktiv`" if f_offline else ""
                        st.markdown(f"**{f_name}**{offline_badge}")
                        st.caption(f"🔗 [{f_url}]({f_url})")
                    with col_top2:
                        with st.popover("🗑️ Löschen", use_container_width=True):
                            st.markdown(f"Feed **'{f_name}'** wirklich entfernen?")
                            if st.button("Löschen bestätigen", key=f"del_feed_{key_hash}", type="primary", use_container_width=True):
                                delete_feed(cat_name, f_url, config=working_config, save_to_disk=False)
                                st.session_state["last_edited_category"] = cat_name
                                st.session_state["has_unsaved_changes"] = True
                                set_dismissible_notice("manage_notice", f"Feed '{f_name}' entfernt.", icon="✅")
                                st.rerun()

                    # Der Bearbeiten-Block: Bleibt bei Änderungen explizit OFFEN!
                    is_feed_expanded = (
                        st.session_state.get("editing_feed_key") == key_hash
                        or st.session_state.get("editing_feed_url") == f_url
                        or st.session_state.get("editing_feed_name") == f_name
                    )
                    with st.expander("🛠️ Feed bearbeiten & testen", expanded=is_feed_expanded):
                        col_ed1, col_ed2 = st.columns(2)
                        with col_ed1:
                            edit_name_val = st.text_input("Name:", value=f_name, key=f"ed_name_{key_hash}")
                            edit_url_val = st.text_input("URL:", value=f_url, key=f"ed_url_{key_hash}")
                        with col_ed2:
                            cat_opts = cat_names if cat_names else ["Allgemein"]
                            default_cat_idx = cat_opts.index(cat_name) if cat_name in cat_opts else 0
                            edit_cat_val = st.selectbox("Kategorie:", options=cat_opts, index=default_cat_idx, key=f"ed_cat_{key_hash}")
                            col_e_kw1, col_e_kw2 = st.columns(2)
                            with col_e_kw1:
                                edit_inc_val = st.text_input(
                                    "🟢 Nur Keywords:",
                                    value=", ".join(f_inc) if isinstance(f_inc, list) else str(f_inc or ""),
                                    key=f"ed_inc_{key_hash}",
                                )
                            with col_e_kw2:
                                edit_exc_val = st.text_input(
                                    "🔴 Ohne Keywords:",
                                    value=", ".join(f_exc) if isinstance(f_exc, list) else str(f_exc or ""),
                                    key=f"ed_exc_{key_hash}",
                                )

                        edit_offline_val = st.checkbox(
                            "📥 Offline-Download für diesen Feed aktivieren (3 Monate Aufbewahrung im Archiv)",
                            value=f_offline,
                            key=f"ed_offline_{key_hash}",
                            help="Speichert automatisch im Hintergrund alle Artikel dieses Feeds inklusive Webseite, Text und Bildern für 3 Monate im Tab 'Archiv'.",
                        )

                        col_eb1, col_eb2 = st.columns([1, 1])
                        with col_eb1:
                            if st.button("🔍 Feed testen", key=f"btn_tst_{key_hash}", use_container_width=True):
                                st.session_state["editing_feed_key"] = key_hash
                                st.session_state["editing_feed_url"] = f_url
                                st.session_state["editing_feed_name"] = f_name
                                st.session_state["editing_feed_category"] = cat_name
                                st.session_state["last_edited_category"] = cat_name
                                with st.spinner("Teste Feed..."):
                                    res = test_feed_connection(edit_url_val.strip())
                                    render_feed_test_result(res)
                        with col_eb2:
                            if st.session_state.get("last_saved_feed_key") == key_hash:
                                render_dismissible_notice(
                                    "Änderungen im Entwurf gemerkt!",
                                    notice_key=f"feed_saved_{key_hash}",
                                    icon="✅",
                                )
                            if st.button("✔️ Im Entwurf merken", key=f"btn_save_feed_{key_hash}", type="primary", use_container_width=True):
                                clean_edit_name = edit_name_val.strip()
                                clean_edit_url = edit_url_val.strip()
                                if clean_edit_name and clean_edit_url:
                                    url_ok, url_err = validate_feed_url(clean_edit_url, resolve_dns=False)
                                    if not url_ok:
                                        st.error(url_err or "Ungültige Feed-URL.")
                                    else:
                                        with st.spinner("Merke Feed-Änderungen im Arbeitsentwurf..."):
                                            update_feed(
                                                cat_name,
                                                f_url,
                                                new_name=clean_edit_name,
                                                new_url=clean_edit_url,
                                                new_category=edit_cat_val.strip(),
                                                include_keywords=edit_inc_val.strip(),
                                                exclude_keywords=edit_exc_val.strip(),
                                                offline_download=bool(edit_offline_val),
                                                config=working_config,
                                                save_to_disk=False,
                                            )
                                            if edit_offline_val:
                                                start_offline_archive_task(
                                                    config=working_config,
                                                    target_feed_url=clean_edit_url,
                                                )
                                            new_hash = _feed_widget_hash(edit_cat_val.strip(), clean_edit_url)
                                            # State merken, damit der Expander OFFEN bleibt!
                                            st.session_state["editing_feed_key"] = new_hash
                                            st.session_state["last_saved_feed_key"] = new_hash
                                            st.session_state["editing_feed_url"] = clean_edit_url
                                            st.session_state["editing_feed_name"] = clean_edit_name
                                            st.session_state["editing_feed_category"] = edit_cat_val.strip()
                                            st.session_state["last_edited_category"] = edit_cat_val.strip()
                                            st.session_state["has_unsaved_changes"] = True
                                            set_dismissible_notice(f"feed_saved_{new_hash}", "Änderungen im Entwurf gemerkt!", icon="✅")
                                            set_dismissible_notice("manage_notice", f"Änderungen für '{clean_edit_name}' im Entwurf gemerkt!", icon="✅")
                                            st.rerun()
                                else:
                                    st.error("Name und URL dürfen nicht leer sein.")

    st.markdown("---")

    # --- Sektion 4: Globale Einstellungen ---
    st.markdown("### ⚙️ Globale Einstellungen")
    with st.container(border=True):
        if st.session_state.get("manage_settings_notice"):
            render_dismissible_notice(st.session_state["manage_settings_notice"], notice_key="manage_settings_notice", icon="⚙️")
        settings = working_config.get("settings", {})

        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            lang_val = st.text_input("Sprache:", value=settings.get("language", "de"), key="inp_set_lang")
        with col_s2:
            style_val = st.text_input("Zusammenfassungs-Stil:", value=settings.get("summary_style", "tldr"), key="inp_set_style")
        with col_s3:
            # Archiv-Aufbewahrung in Tagen (Wochenschema vollständig eliminiert!)
            curr_retention = int(settings.get("archive_retention_days", DEFAULT_ARCHIVE_RETENTION_DAYS))
            retention_val = st.number_input(
                "Archiv-Aufbewahrung (Tage):",
                min_value=1,
                max_value=365,
                value=curr_retention,
                help="Archivierte Artikel, die älter als diese Anzahl an Tagen sind, werden beim morgendlichen Lauf automatisch aus der Datenbank gelöscht.",
                key="inp_set_retention_days",
            )

        st.divider()

        filter_ads_val = st.checkbox(
            "Ad-Filter aktiv (Ad-Artikel komplett herausfiltern)",
            value=bool(settings.get("filter_ads", True)),
            help="Wenn aktiviert, werden erkannte Ad-Artikel anhand der konfigurierten Ad-Patterns nicht in die Feeds übernommen.",
            key="inp_set_filter_ads",
        )

        curr_ad_pat = settings.get("ad_patterns", [])
        if isinstance(curr_ad_pat, list):
            ad_pat_initial = "\n".join(str(p) for p in curr_ad_pat)
        else:
            ad_pat_initial = str(curr_ad_pat or "")

        ad_pat_val = st.text_area(
            "Ad-Patterns (zeilenweise):",
            value=ad_pat_initial,
            help="Reguläre Ausdrücke zur Erkennung von Ads. Jedes Muster in einer eigenen Zeile.",
            placeholder="z. B. ^(?:anzeige|werbung)\\s*[:\\-\\|•]",
            key="inp_set_ad_patterns",
            height=130,
        )

        if st.button("✔️ Einstellungen im Entwurf übernehmen", key="btn_apply_settings", use_container_width=True):
            with st.spinner("Übernehme Einstellungen im Arbeitsentwurf..."):
                settings.pop("streamlit_app_url", None)
                settings["language"] = lang_val.strip()
                settings["archive_retention_days"] = int(retention_val)
                settings["summary_style"] = style_val.strip()
                settings["filter_ads"] = bool(filter_ads_val)
                settings.pop("strip_ads", None)
                settings.pop("ad_keywords", None)
                settings["ad_patterns"] = _parse_ad_patterns(ad_pat_val)

                # Wochenschema sicherstellen, dass es weg ist
                settings.pop("max_article_age_weeks", None)
                settings.pop("max_age_weeks", None)
                settings.pop("batch_sync_interval_minutes", None)
                working_config["settings"] = settings
                st.session_state["has_unsaved_changes"] = True
                set_dismissible_notice("manage_settings_notice", "Einstellungen im Entwurf übernommen.", icon="⚙️")
                set_dismissible_notice("manage_notice", "Einstellungen im Entwurf übernommen.", icon="✅")
                st.rerun()

    # --- Sektion 5: Live settings.yaml Preview ---
    with st.expander("⚙️ Vorschau der settings.yaml", expanded=False):
        settings_preview = {k: v for k, v in working_config.get("settings", {}).items() if k not in PROMPT_KEYS}
        st.code(yaml.dump(settings_preview, allow_unicode=True, sort_keys=False), language="yaml")

