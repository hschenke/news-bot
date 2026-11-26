"""
UI Styles and client-side CSS module for News Bot.
Encapsulates all layout, responsive design, anti-stale optimizations, and typography rules.
"""

from __future__ import annotations

import time
import streamlit as st


def embed_client_script(js_code: str) -> None:
    """Führt Hilfsskripte (z. B. Cookie/Storage-Sync) modern und unsichtbar via st.html aus (Chrome & Firefox)."""
    if not js_code or not js_code.strip():
        return
    html_wrapper = (
        f"<div style='display:none !important;width:0 !important;height:0 !important;"
        f"margin:0 !important;padding:0 !important;overflow:hidden !important;border:none !important;'>"
        f"<script>{js_code}</script></div>"
    )
    st.html(html_wrapper, unsafe_allow_javascript=True)


_VALID_NOTICE_LEVELS: frozenset[str] = frozenset({"success", "warning", "error"})


def set_dismissible_notice(
    notice_key: str,
    message: str,
    icon: str = "✅",
    level: str = "success",
) -> None:
    """
    Zentrale Funktion zum Setzen einer persistenten Hinweis-/Statusmeldung über alle Seiten hinweg.
    Unterstützt die Stufen 'success' (grün), 'warning' (gelb/orange) und 'error' (rot).
    Erhöht automatisch die Sequenznummer, damit auch identische Folgemeldungen nach einem
    vorherigen Schließen per '✖' zuverlässig wieder eingeblendet werden.
    """
    if not notice_key or not message or not str(message).strip():
        return
    resolved_level = level if level in _VALID_NOTICE_LEVELS else "success"
    st.session_state[notice_key] = str(message).strip()
    st.session_state[f"_notice_icon_{notice_key}"] = icon
    st.session_state[f"_notice_level_{notice_key}"] = resolved_level
    curr_seq = int(st.session_state.get(f"_notice_seq_{notice_key}", 0))
    st.session_state[f"_notice_seq_{notice_key}"] = curr_seq + 1


def render_dismissible_notice(
    notice_text: str | None = None,
    notice_key: str = "global_notice",
    icon: str = "✅",
    level: str | None = None,
) -> None:
    """
    Zentrale Funktion zum Rendern persistenter Statusmeldungen (success / warning / error).
    Verschwindet NICHT automatisch, sondern ausschließlich bei Klick auf das '✖'.
    """
    resolved_text = notice_text if notice_text is not None else st.session_state.get(notice_key)
    if not resolved_text or not str(resolved_text).strip():
        return
    import html
    import hashlib

    resolved_str = str(resolved_text).strip()
    if st.session_state.get(f"_notice_last_text_{notice_key}") != resolved_str:
        st.session_state[f"_notice_last_text_{notice_key}"] = resolved_str
        st.session_state[f"_notice_seq_{notice_key}"] = int(st.session_state.get(f"_notice_seq_{notice_key}", 0)) + 1

    resolved_icon = str(st.session_state.get(f"_notice_icon_{notice_key}") or icon)
    raw_level = level if level is not None else st.session_state.get(f"_notice_level_{notice_key}", "success")
    resolved_level = str(raw_level) if str(raw_level) in _VALID_NOTICE_LEVELS else "success"
    seq = int(st.session_state.get(f"_notice_seq_{notice_key}", 1))
    escaped_text = html.escape(resolved_str)
    content_hash = hashlib.md5(f"{escaped_text}_{resolved_level}_{seq}".encode("utf-8")).hexdigest()[:8]
    banner_id = f"notice-banner-{notice_key}-{content_hash}"
    btn_id = f"notice-close-btn-{notice_key}-{content_hash}"
    html_markup = f"""
    <div id="{banner_id}" class="persistent-dismissible-notice notice-{resolved_level}" data-banner-id="{banner_id}" data-notice-level="{resolved_level}">
        <div class="notice-content">
            <span class="notice-icon">{resolved_icon}</span>
            <span class="notice-message">{escaped_text}</span>
        </div>
        <button id="{btn_id}" class="notice-close-btn" type="button" onclick="try{{sessionStorage.setItem('{banner_id}','dismissed');}}catch(e){{}}var el=document.getElementById('{banner_id}');if(el){{el.style.display='none';el.remove();}}" title="Hinweis schließen">✖</button>
    </div>
    <script>
    (function() {{
        var bId = "{banner_id}";
        var btnId = "{btn_id}";
        try {{
            if (sessionStorage.getItem(bId) === "dismissed") {{
                var el = document.getElementById(bId);
                if (el) {{
                    el.style.display = "none";
                    el.remove();
                }}
                return;
            }}
        }} catch(e) {{}}
        var btn = document.getElementById(btnId);
        if (btn) {{
            btn.onclick = function() {{
                try {{ sessionStorage.setItem(bId, "dismissed"); }} catch(e) {{}}
                var el = document.getElementById(bId);
                if (el) {{
                    el.style.display = "none";
                    el.remove();
                }}
            }};
        }}
    }})();
    </script>
    """
    st.html(html_markup, unsafe_allow_javascript=True)


def release_action_overlay() -> None:
    """Ensure that the loading overlay is released once the page render completes."""
    nonce = time.time()
    embed_client_script(f"""
        (function() {{
            var _t = '{nonce}';
            if (window._newsBotHideActionOverlay) {{
                window._newsBotHideActionOverlay();
            }}
            var el = document.getElementById('news-bot-loading-overlay');
            if (el) {{
                el.classList.remove('active');
                el.style.display = 'none';
                el.style.opacity = '0';
                el.style.visibility = 'hidden';
                el.style.pointerEvents = 'none';
            }}
        }})();
    """)



def apply_custom_styles() -> None:
    """Applies high-performance custom CSS to the Streamlit app."""
    st.markdown("""
<style>
    /* Main container spacing - balanced clean layout */
    .block-container {
        padding-top: 2.25rem !important;
        padding-bottom: 2rem !important;
        padding-left: 1.25rem !important;
        padding-right: 1.25rem !important;
        max-width: 1250px;
    }

    /* Vertical block flow: uniform harmonious gap */
    [data-testid="stVerticalBlock"] {
        gap: 0.55rem !important;
    }

    /* Sidebar vertical flow */
    section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap: 0.45rem !important;
    }

    /* Divider rules: visible, balanced dividers */
    hr, [data-testid="stDivider"], .stMarkdown hr {
        margin-top: 0.65rem !important;
        margin-bottom: 0.65rem !important;
        border: none !important;
        border-top: 1px solid rgba(100, 116, 139, 0.35) !important;
    }

    /* Sidebar dividers: crisp, unmistakably visible with balanced breathing room */
    section[data-testid="stSidebar"] hr,
    section[data-testid="stSidebar"] [data-testid="stDivider"],
    section[data-testid="stSidebar"] [data-testid="stDivider"] hr,
    section[data-testid="stSidebar"] .stMarkdown hr {
        margin-top: 0.35rem !important;
        margin-bottom: 0.35rem !important;
        border: none !important;
        border-top: 1.5px solid rgba(100, 116, 139, 0.55) !important;
        display: block !important;
        width: 100% !important;
        opacity: 1 !important;
        visibility: visible !important;
    }
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(hr) {
        min-height: 1.5px !important;
        overflow: visible !important;
        margin-top: 0.25rem !important;
        margin-bottom: 0.35rem !important;
    }

    /* Divider directly below custom navigation (Feedly button): balanced breathing room */
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.custom-nav-container) + [data-testid="stElementContainer"]:has(hr) {
        margin-top: 0.45rem !important;
    }

    /* Snug divider directly below sidebar captions (fixes Gap 1: 24h Newsfeed and Last timestamp) */
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.stCaption) + [data-testid="stElementContainer"]:has(hr),
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stCaptionContainer"]) + [data-testid="stElementContainer"]:has(hr) {
        margin-top: 0.15rem !important;
    }
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.stCaption) + [data-testid="stElementContainer"]:has(hr) hr,
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stCaptionContainer"]) + [data-testid="stElementContainer"]:has(hr) hr {
        margin-top: 0.15rem !important;
    }

    /* Sidebar action buttons (e.g. Feeds neu laden, Abmelden): balanced spacing from dividers */
    section[data-testid="stSidebar"] .stButton {
        margin-top: 0.15rem !important;
        margin-bottom: 0.15rem !important;
    }
    section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(hr) + [data-testid="stElementContainer"]:has(.stButton) {
        margin-top: 0.35rem !important;
    }

    /* Headings & Section Typography: uniform vertical rhythm */
    h1 {
        margin-top: 0 !important;
        margin-bottom: 0.15rem !important;
        line-height: 1.2 !important;
        padding: 0 !important;
    }
    h2, .stSubheader {
        margin-top: 0.35rem !important;
        margin-bottom: 0.25rem !important;
        padding: 0 !important;
    }
    h3, .stMarkdown h3 {
        margin-top: 1.0rem !important;
        margin-bottom: 0.4rem !important;
        padding: 0 !important;
    }
    h4, h5, h6 {
        margin-top: 0.35rem !important;
        margin-bottom: 0.25rem !important;
        padding: 0 !important;
    }

    /* Captions: consistent, uniform margins */
    .stCaption, [data-testid="stCaptionContainer"] {
        margin-top: 0.1rem !important;
        margin-bottom: 0.05rem !important;
        line-height: 1.35 !important;
    }
    .stCaption p, [data-testid="stCaptionContainer"] p {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
    }

    section[data-testid="stSidebar"] .stCaption,
    section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
        margin-top: 0.1rem !important;
        margin-bottom: 0 !important;
    }
    section[data-testid="stSidebar"] .stCaption p {
        margin: 0 !important;
        padding: 0 !important;
    }

    /* Gap 2: close excessive whitespace between Stand caption and tabs nav */
    .block-container [data-testid="stElementContainer"]:has(.stCaption) {
        margin-bottom: 0 !important;
    }
    .block-container [data-testid="stElementContainer"]:has(.stTabs) {
        margin-top: -0.15rem !important;
    }

    /* Streamlit tabs & panels: tight, uniform spacing above and below tab bar */
    .stTabs, [data-testid="stTabs"] {
        margin-top: 0 !important;
    }
    .stTabs [role="tablist"],
    .stTabs [data-baseweb="tab-list"],
    [data-testid="stTabs"] [role="tablist"],
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 0.45rem !important;
        margin-top: 0 !important;
        margin-bottom: 0.15rem !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }
    .stTabs [data-baseweb="tab"],
    [data-testid="stTab"] {
        font-size: 0.95rem;
        font-weight: 500;
        padding: 0.45rem 0.9rem;
        border-radius: 0.375rem;
    }

    /* Gap 3: Eliminate Streamlit 24px paddingTop on tab panels below the nav divider */
    [data-testid="stTabPanel"],
    .react-aria-TabPanel,
    [role="tabpanel"],
    [data-baseweb="tab-panel"],
    [data-testid="stTabContent"] {
        padding-top: 0.15rem !important;
        padding-bottom: 0 !important;
        padding-left: 0 !important;
        padding-right: 0 !important;
        margin-top: 0 !important;
    }
    [data-testid="stTabPanel"] > div > [data-testid="stVerticalBlock"],
    [role="tabpanel"] > div > [data-testid="stVerticalBlock"] {
        gap: 0.45rem !important;
    }
    [data-testid="stTabPanel"] h2,
    [data-testid="stTabPanel"] .stSubheader,
    [role="tabpanel"] h2,
    [role="tabpanel"] .stSubheader {
        margin-top: 0.15rem !important;
        margin-bottom: 0.2rem !important;
        padding-top: 0 !important;
    }
    [data-testid="stTabPanel"] .stCaption,
    [role="tabpanel"] .stCaption {
        margin-top: 0 !important;
        margin-bottom: 0.25rem !important;
    }

    /* Compact alerts */
    [data-testid="stAlert"] {
        padding: 0.5rem 0.8rem !important;
        margin-top: 0.2rem !important;
        margin-bottom: 0.2rem !important;
    }
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] p {
        margin: 0 !important;
        line-height: 1.35 !important;
    }

    /* Expander spacing: uniform comfortable separation */
    [data-testid="stExpander"] {
        margin-top: 0.35rem !important;
        margin-bottom: 0.6rem !important;
        border-radius: 0.5rem !important;
    }
    [data-testid="stExpander"] details summary {
        padding: 0.45rem 0.85rem !important;
    }
    [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
        padding: 0.65rem 0.85rem !important;
    }
    [data-testid="stExpanderDetails"] [data-testid="stVerticalBlock"] {
        gap: 0.4rem !important;
    }
    [data-testid="stExpanderDetails"] .stCaption {
        margin-top: 0 !important;
        margin-bottom: 0.15rem !important;
    }
    [data-testid="stExpanderDetails"] .stCaption p {
        margin: 0 !important;
        padding: 0 !important;
    }
    [data-testid="stExpanderDetails"] [data-testid="stWidgetLabel"] {
        margin-top: 0.15rem !important;
        margin-bottom: 0.15rem !important;
        min-height: 0 !important;
    }
    [data-testid="stExpanderDetails"] [data-testid="stWidgetLabel"] label,
    [data-testid="stExpanderDetails"] [data-testid="stWidgetLabel"] p {
        margin: 0 !important;
        padding: 0 !important;
    }

    /* Bordered containers (Status box, Feed cards) */
    [data-testid="stVerticalBlockBorderWrapper"] {
        margin-top: 0.35rem !important;
        margin-bottom: 0.65rem !important;
        padding: 0.65rem 0.85rem !important;
    }

    /* Compact buttons */
    .stButton button {
        padding: 0.35rem 0.75rem !important;
        min-height: 2.15rem !important;
    }

    /* Invisible helper iframes */
    [data-testid="stIFrame"]:has(iframe[height="0"]),
    [data-testid="stIFrame"]:has(iframe[height="1"]),
    [data-testid="stCustomComponentV1"]:has(iframe[height="0"]),
    iframe[height="0"], iframe[height="1"],
    iframe[width="0"], iframe[width="1"] {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        border: none !important;
        margin: 0 !important;
        padding: 0 !important;
        visibility: hidden !important;
        position: absolute !important;
        pointer-events: none !important;
    }

    /* Header link anchors */
    [data-testid="stHeaderActionElements"],
    .stMarkdown a.anchor-link,
    a.header-anchor,
    h1 a, h2 a, h3 a, h4 a, h5 a, h6 a {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
        opacity: 0 !important;
        width: 0 !important;
        height: 0 !important;
    }

    /* Skeleton loaders */
    [data-testid="stSkeleton"],
    [data-testid="stSkeletonElement"],
    .stSkeleton {
        display: none !important;
        opacity: 0 !important;
        visibility: hidden !important;
    }

    /* Anti-stale state: prevents greying / fading out during reruns */
    [data-stale="true"],
    [data-testid="stElementContainer"][data-stale="true"],
    [data-testid="stVerticalBlockBorderWrapper"][data-stale="true"],
    [data-testid="stVerticalBlock"][data-stale="true"],
    [data-testid="stHorizontalBlock"][data-stale="true"],
    .stApp [data-stale="true"],
    .stApp div[data-stale="true"] {
        opacity: 1 !important;
        transition: none !important;
        filter: none !important;
    }

    /* Keep buttons and feedback visible and active */
    [data-testid="stFeedback"] button:disabled,
    [data-testid="stFeedback"] button[data-disabled="true"],
    [data-testid="stFeedback"] button[disabled],
    div[class*="st-key-read_"] button:disabled,
    div[class*="st-key-read_"] button[data-disabled="true"],
    div[class*="st-key-read_"] button[disabled] {
        opacity: 1 !important;
        cursor: pointer !important;
    }

    .stApp[data-test-script-state="running"] [data-testid="stVerticalBlockBorderWrapper"],
    .stApp[data-test-script-state="running"] [data-testid="stElementContainer"],
    .stApp[data-test-script-state="running"] [data-testid="stFeedback"],
    .stApp[data-test-script-state="running"] div[class*="st-key-read_"] {
        opacity: 1 !important;
        filter: none !important;
    }

    /* Card container styling */
    [data-testid="stVerticalBlockBorderWrapper"] {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
        overflow: hidden !important;
        border-radius: 0.5rem !important;
        transition: transform 0.1s ease, box-shadow 0.1s ease !important;
    }

    /* Action bar inside article cards (Desktop & Mobile universal fit) */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])),
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])),
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        align-items: center !important;
        justify-content: space-between !important;
        width: 100% !important;
        max-width: 100% !important;
        min-width: 0 !important;
        gap: 0.35rem !important;
        overflow: visible !important;
        overflow-x: hidden !important;
        box-sizing: border-box !important;
        margin-top: 0.4rem !important;
    }

    /* Alle Spalten in der Card-Action-Bar: Streamlit 8rem / 100% min-width zuverlässig aufheben */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"] {
        min-width: 0 !important;
        width: auto !important;
        max-width: none !important;
        box-sizing: border-box !important;
    }

    /* Links: Bewertungs-Daumen / Download-Aktion */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child,
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:first-child,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:first-child,
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:first-child,
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:first-child {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: flex-start !important;
        flex: 0 1 auto !important;
        width: auto !important;
        min-width: 0 !important;
        max-width: calc(100% - 2.5rem) !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child [data-testid="stVerticalBlock"],
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:first-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:first-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:first-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:first-child [data-testid="stVerticalBlock"] {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: flex-start !important;
        width: auto !important;
        min-width: 0 !important;
        gap: 0 !important;
    }
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:first-child [data-testid="stElementContainer"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:first-child [data-testid="stElementContainer"],
    div[class*="st-key-fb_"] {
        width: auto !important;
        min-width: 0 !important;
        max-width: 100% !important;
    }

    /* Rechts: Gelesen-Button / Löschen-Icon immer sichtbar & bündig am rechten Rand */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child,
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:last-child,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:last-child,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:last-child,
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:last-child,
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:last-child {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: flex-end !important;
        flex: 0 0 auto !important;
        width: auto !important;
        min-width: 0 !important;
        max-width: none !important;
        margin-left: auto !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"],
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:last-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:last-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:last-child [data-testid="stVerticalBlock"] {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: flex-end !important;
        width: auto !important;
        min-width: 0 !important;
        gap: 0 !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child .stButton,
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:last-child .stButton,
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child div[class*="st-key-read_"],
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:last-child div[class*="st-key-read_"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:last-child .stButton,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:last-child .stButton,
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:last-child div[class*="st-key-read_"],
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:last-child div[class*="st-key-read_"],
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:last-child .stButton,
    [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:last-child div[class*="st-key-read_"] {
        display: inline-flex !important;
        justify-content: flex-end !important;
        align-items: center !important;
        margin-left: auto !important;
        margin-right: 0 !important;
        width: auto !important;
        min-width: 0 !important;
        flex: 0 0 auto !important;
    }

    /* Badges */
    .news-badge {
        display: inline-block;
        padding: 0.2rem 0.5rem;
        font-size: 0.75rem;
        font-weight: 600;
        border-radius: 0.25rem;
        margin-right: 0.4rem;
        margin-bottom: 0.3rem;
    }
    .news-badge-cat {
        background-color: rgba(37, 99, 235, 0.12);
        color: #2563EB;
        border: 1px solid rgba(37, 99, 235, 0.25);
    }
    .news-badge-source {
        background-color: rgba(100, 116, 139, 0.12);
        color: #475569;
        border: 1px solid rgba(100, 116, 139, 0.2);
    }
    .news-badge-date {
        background-color: rgba(148, 163, 184, 0.1);
        color: #64748B;
    }

    /* Sidebar Header & Version Badge Spacing */
    .sidebar-header-container {
        display: flex !important;
        align-items: center !important;
        gap: 12px !important;
        margin-top: 0.2rem !important;
        margin-bottom: 0.25rem !important;
    }
    .sidebar-header-title {
        font-size: 1.35rem !important;
        font-weight: 700 !important;
        line-height: 1.2 !important;
        margin: 0 !important;
        display: inline-flex !important;
        align-items: center !important;
    }
    .sidebar-version-badge {
        display: inline-flex !important;
        align-items: center !important;
        margin-left: 10px !important;
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        padding: 2px 8px !important;
        border-radius: 6px !important;
        background: rgba(37, 99, 235, 0.1) !important;
        border: 1px solid rgba(37, 99, 235, 0.25) !important;
        color: #2563eb !important;
        letter-spacing: 0.02em !important;
        vertical-align: middle !important;
        white-space: nowrap !important;
    }

    /* Sidebar Navigation */
    .custom-nav-container {
        display: flex !important;
        flex-direction: column !important;
        gap: 0.45rem !important;
        margin-top: 0.35rem !important;
        margin-bottom: 0.65rem !important;
        width: 100% !important;
    }
    .custom-nav-container .custom-nav-btn:last-child {
        margin-bottom: 0.25rem !important;
    }
    .custom-nav-btn {
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 100% !important;
        padding-top: 0.4rem !important;
        padding-bottom: 0.4rem !important;
        padding-left: 0.65rem !important;
        padding-right: 0.65rem !important;
        min-height: 2.25rem !important;
        line-height: 1.2 !important;
        font-size: 0.88rem !important;
        font-weight: 500 !important;
        border-radius: 0.45rem !important;
        border: 1px solid rgba(128, 128, 128, 0.22) !important;
        background-color: transparent !important;
        color: inherit !important;
        cursor: pointer !important;
        transition: all 0.15s ease-in-out !important;
        text-align: center !important;
        box-sizing: border-box !important;
        user-select: none !important;
    }
    .custom-nav-btn:hover:not(.disabled-nav-btn) {
        background-color: rgba(37, 99, 235, 0.08) !important;
        border-color: #2563EB !important;
        color: #2563EB !important;
    }
    .custom-nav-btn.active-nav-tab {
        background-color: #2563EB !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        border: 1px solid #1d4ed8 !important;
        box-shadow: 0 1px 3px rgba(37, 99, 235, 0.3) !important;
    }
    .custom-nav-btn.disabled-nav-btn {
        opacity: 0.42 !important;
        cursor: not-allowed !important;
        border: 1px dashed rgba(128, 128, 128, 0.3) !important;
        color: rgba(128, 128, 128, 0.8) !important;
        background-color: transparent !important;
    }


    /* KPI chips */
    .kpi-container {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin: 0.4rem 0;
    }
    .kpi-chip {
        padding: 0.25rem 0.5rem;
        border-radius: 0.375rem;
        font-size: 0.8rem;
        font-weight: 600;
        background-color: rgba(128, 128, 128, 0.1);
    }

    /* Bewertungs-Daumen kompakt & direkt unterm Text platzieren */
    [data-testid="stFeedback"] {
        margin-top: 0.25rem !important;
        margin-bottom: 0 !important;
        padding: 0 !important;
        display: inline-flex !important;
        align-items: center !important;
    }
    [data-testid="stFeedback"] button {
        padding: 0.25rem 0.45rem !important;
        min-height: 2rem !important;
        border-radius: 8px !important;
        transition: all 0.15s ease-in-out !important;
    }
    /* Nicht ausgewählter Button: dezent & Outline */
    [data-testid="stFeedback"] button[data-testid="stFeedbackButton"]:not([data-testid="stFeedbackButtonActive"]):not([aria-checked="true"]) {
        color: #94a3b8 !important;
        background-color: transparent !important;
    }
    [data-testid="stFeedback"] button[data-testid="stFeedbackButton"]:not([data-testid="stFeedbackButtonActive"]):not([aria-checked="true"]) span {
        font-variation-settings: 'FILL' 0, 'wght' 400 !important;
    }
    [data-testid="stFeedback"] button:hover:not(:disabled) {
        background-color: rgba(128, 128, 128, 0.12) !important;
    }

    /* Ausgewählter Like-Button (Daumen hoch): Komplett ausgefülltes Icon & grüner Akzent */
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="up" i],
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="up" i] {
        color: #16a34a !important;
        background-color: rgba(22, 163, 74, 0.18) !important;
        border: 1px solid rgba(22, 163, 74, 0.4) !important;
    }
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="up" i] span,
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="up" i] span,
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="up" i] [data-testid="stIconMaterial"],
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="up" i] [data-testid="stIconMaterial"] {
        font-variation-settings: 'FILL' 1, 'wght' 700 !important;
        color: #16a34a !important;
    }

    /* Ausgewählter Dislike-Button (Daumen runter): Komplett ausgefülltes Icon & roter Akzent */
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="down" i],
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="down" i] {
        color: #dc2626 !important;
        background-color: rgba(220, 38, 38, 0.18) !important;
        border: 1px solid rgba(220, 38, 38, 0.4) !important;
    }
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="down" i] span,
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="down" i] span,
    [data-testid="stFeedback"] button[data-testid="stFeedbackButtonActive"][aria-label*="down" i] [data-testid="stIconMaterial"],
    [data-testid="stFeedback"] button[aria-checked="true"][aria-label*="down" i] [data-testid="stIconMaterial"] {
        font-variation-settings: 'FILL' 1, 'wght' 700 !important;
        color: #dc2626 !important;
    }

    /* Gelesen-Symbol Styling: Randlos, transparent, dezent wie st.feedback */
    div[class*="st-key-read_"] {
        display: inline-flex !important;
        justify-content: flex-end !important;
        align-items: center !important;
        margin: 0 !important;
        margin-left: auto !important;
        padding: 0 !important;
        width: auto !important;
    }
    div[class*="st-key-read_"] button {
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        outline: none !important;
        padding: 0 !important;
        min-height: 2rem !important;
        height: 2rem !important;
        width: 2rem !important;
        min-width: 2rem !important;
        max-width: 2rem !important;
        max-height: 2rem !important;
        border-radius: 50% !important;
        color: #94a3b8 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        line-height: 1 !important;
        margin: 0 !important;
        margin-left: auto !important;
        transition: background-color 0.15s ease-in-out, color 0.15s ease-in-out !important;
        overflow: hidden !important;
    }
    div[class*="st-key-read_"] button:hover {
        background-color: rgba(34, 197, 94, 0.15) !important;
        color: #16a34a !important;
    }
    div[class*="st-key-read_"] button:active,
    div[class*="st-key-read_"] button:focus {
        background-color: rgba(34, 197, 94, 0.22) !important;
        border: none !important;
        box-shadow: none !important;
        outline: none !important;
        color: #16a34a !important;
    }
    div[class*="st-key-read_"] button [data-testid="stIconMaterial"],
    div[class*="st-key-read_"] button span {
        color: #16a34a !important;
        font-variation-settings: 'FILL' 1, 'wght' 600 !important;
    }

    /* Red delete button styling for settings backups */
    div[class*="st-key-pop_del_sb_"] button {
        color: #ef4444 !important;
        border-color: rgba(239, 68, 68, 0.4) !important;
        background-color: transparent !important;
    }
    div[class*="st-key-pop_del_sb_"] button:hover {
        color: #dc2626 !important;
        border-color: #ef4444 !important;
        background-color: rgba(239, 68, 68, 0.1) !important;
    }
    div[class*="st-key-pop_del_sb_"] button [data-testid="stIconMaterial"],
    div[class*="st-key-pop_del_sb_"] button span {
        color: #ef4444 !important;
    }

    /* Mobile Responsive Rules */
    @media (max-width: 768px) {
        /* Suchleiste auf Mobile: Eingabefeld, Reset und Go in einer Zeile bündig halten */
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: flex-end !important;
            width: 100% !important;
            max-width: 100% !important;
            gap: 0.35rem !important;
            box-sizing: border-box !important;
        }
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="stColumn"]:has(.st-key-input_search_query),
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="column"]:has(.st-key-input_search_query) {
            min-width: 0 !important;
            flex: 1 1 0 !important;
            width: auto !important;
        }
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="stColumn"]:has(.st-key-btn_search_clear),
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="column"]:has(.st-key-btn_search_clear) {
            min-width: 2.5rem !important;
            max-width: 2.75rem !important;
            flex: 0 0 2.5rem !important;
        }
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="stColumn"]:has(.st-key-btn_search_go),
        [data-testid="stHorizontalBlock"]:has(.st-key-input_search_query) > [data-testid="column"]:has(.st-key-btn_search_go) {
            min-width: 2.5rem !important;
            max-width: 2.75rem !important;
            flex: 0 0 2.5rem !important;
        }

        /* Kompaktere Seiten- & Expander-Abstände auf Smartphones für maximale Card-Breite */
        .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
        }
        [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
            padding: 0.5rem 0.6rem !important;
        }

        /* Äußere Card-Columns innerhalb von Expandern auf Mobile auf 100% Breite stacken (ohne innere Action-Bar-Columns zu beeinflussen) */
        [data-testid="stExpander"] [data-testid="stColumn"]:has([data-testid="stVerticalBlockBorderWrapper"]),
        [data-testid="stExpander"] [data-testid="column"]:has([data-testid="stVerticalBlockBorderWrapper"]),
        [data-testid="stExpander"] [data-testid="stColumn"]:has([data-testid="stHorizontalBlock"]),
        [data-testid="stExpander"] [data-testid="column"]:has([data-testid="stHorizontalBlock"]) {
            min-width: 100% !important;
            width: 100% !important;
            flex: 1 1 100% !important;
            max-width: 100% !important;
            box-sizing: border-box !important;
        }

        /* Feedback-Daumen & Gelesen-Symbol innerhalb der Card AUF MOBILE IMMER in 1 Zeile ohne Scrollen bündig halten */
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"],
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])),
        [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])),
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            justify-content: space-between !important;
            width: 100% !important;
            max-width: 100% !important;
            min-width: 0 !important;
            gap: 0.35rem !important;
            overflow: visible !important;
            overflow-x: hidden !important;
            box-sizing: border-box !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"],
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"],
        [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"],
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"],
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"] {
            display: flex !important;
            flex-direction: row !important;
            align-items: center !important;
            min-width: 0 !important;
            max-width: none !important;
            width: auto !important;
            flex: 0 0 auto !important;
            box-sizing: border-box !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child,
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child,
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:first-child,
        [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:first-child,
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:first-child,
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:first-child {
            justify-content: flex-start !important;
            flex: 0 1 auto !important;
            min-width: 0 !important;
            max-width: calc(100% - 2.5rem) !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child,
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] > [data-testid="column"]:last-child,
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="stColumn"]:last-child,
        [data-testid="stHorizontalBlock"]:has(> [data-testid="column"] div[class*="st-key-read_"]):not(:has([data-testid="stHorizontalBlock"])) > [data-testid="column"]:last-child,
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="stColumn"]:last-child,
        [data-testid="stHorizontalBlock"][data-test-wrap="false"]:has(div[class*="st-key-read_"]) > [data-testid="column"]:last-child {
            justify-content: flex-end !important;
            flex: 0 0 auto !important;
            min-width: 0 !important;
            margin-left: auto !important;
        }
    }

    /* Centered Action Loading Overlay: Greys out entire page, disables interaction, shows central spinner */
    #news-bot-loading-overlay {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        background-color: rgba(15, 23, 42, 0.72) !important;
        backdrop-filter: blur(5px) !important;
        -webkit-backdrop-filter: blur(5px) !important;
        z-index: 9999999 !important;
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        justify-content: center !important;
        color: #ffffff !important;
        pointer-events: none !important;
        opacity: 0 !important;
        visibility: hidden !important;
        transition: opacity 0.15s ease-in-out, visibility 0.15s ease-in-out !important;
    }
    #news-bot-loading-overlay.active {
        opacity: 1 !important;
        visibility: visible !important;
        pointer-events: all !important;
    }
    .action-spinner-circle {
        width: 58px !important;
        height: 58px !important;
        border: 5px solid rgba(255, 255, 255, 0.22) !important;
        border-top-color: #38bdf8 !important;
        border-radius: 50% !important;
        animation: spin-action-overlay 0.85s linear infinite !important;
        margin-bottom: 1.25rem !important;
    }
    @keyframes spin-action-overlay {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    .action-spinner-title {
        font-size: 1.35rem !important;
        font-weight: 700 !important;
        color: #f8fafc !important;
        text-align: center !important;
        max-width: 85% !important;
        line-height: 1.4 !important;
        text-shadow: 0 2px 6px rgba(0, 0, 0, 0.6) !important;
    }
    .action-spinner-subtitle {
        font-size: 0.95rem !important;
        color: #cbd5e1 !important;
        margin-top: 0.5rem !important;
        text-align: center !important;
        max-width: 80% !important;
        line-height: 1.4 !important;
    }
    /* Hide all Streamlit toasts globally */
    [data-testid="stToast"],
    .stToast {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        height: 0 !important;
        width: 0 !important;
        overflow: hidden !important;
    }

    /* Strikte Formatierung für deaktivierte Buttons (z. B. 💾 Gespeichert) */
    button:disabled,
    button[disabled],
    [data-testid="stBaseButton-secondary"]:disabled,
    [data-testid="stBaseButton-primary"]:disabled {
        opacity: 0.45 !important;
        cursor: not-allowed !important;
        pointer-events: none !important;
        filter: grayscale(0.6) !important;
    }

    /* Persistentes Hinweisfeld mit sofort schließendem X-Button ohne Server-Roundtrip */
    .persistent-dismissible-notice {
        display: flex !important;
        align-items: center !important;
        justify-content: space-between !important;
        background-color: #ecfdf5 !important;
        border: 1px solid #6ee7b7 !important;
        border-left: 4px solid #059669 !important;
        border-radius: 8px !important;
        padding: 0.5rem 0.85rem !important;
        margin-bottom: 0.5rem !important;
        color: #065f46 !important;
        font-size: 0.92rem !important;
        line-height: 1.35 !important;
        box-sizing: border-box !important;
        width: 100% !important;
    }
    .persistent-dismissible-notice .notice-content {
        display: flex !important;
        align-items: center !important;
        gap: 0.6rem !important;
        flex: 1 1 auto !important;
        font-weight: 600 !important;
        color: #065f46 !important;
    }
    .persistent-dismissible-notice .notice-icon {
        font-size: 1.15rem !important;
        line-height: 1 !important;
        flex-shrink: 0 !important;
    }
    .persistent-dismissible-notice .notice-message {
        color: #065f46 !important;
        font-weight: 600 !important;
    }
    .persistent-dismissible-notice .notice-close-btn {
        background: transparent !important;
        border: none !important;
        color: #047857 !important;
        font-size: 1.25rem !important;
        font-weight: 700 !important;
        line-height: 1 !important;
        padding: 0.2rem 0.6rem !important;
        margin-left: 0.75rem !important;
        cursor: pointer !important;
        border-radius: 4px !important;
        transition: color 0.15s ease, background-color 0.15s ease !important;
        flex-shrink: 0 !important;
    }
    .persistent-dismissible-notice .notice-close-btn:hover {
        color: #064e3b !important;
        background-color: rgba(5, 150, 105, 0.15) !important;
    }

    /* Warnhinweis (z. B. Cloud-DB OK, aber GitHub-Sync 401/403) */
    .persistent-dismissible-notice.notice-warning {
        background-color: #fffbeb !important;
        border: 1px solid #fcd34d !important;
        border-left: 4px solid #d97706 !important;
        color: #92400e !important;
    }
    .persistent-dismissible-notice.notice-warning .notice-content,
    .persistent-dismissible-notice.notice-warning .notice-message {
        color: #92400e !important;
    }
    .persistent-dismissible-notice.notice-warning .notice-close-btn {
        color: #b45309 !important;
    }
    .persistent-dismissible-notice.notice-warning .notice-close-btn:hover {
        color: #78350f !important;
        background-color: rgba(217, 119, 6, 0.15) !important;
    }

    /* Fehlerhinweis (z. B. weder Cloud-DB noch GitHub gespeichert) */
    .persistent-dismissible-notice.notice-error {
        background-color: #fef2f2 !important;
        border: 1px solid #fca5a5 !important;
        border-left: 4px solid #dc2626 !important;
        color: #991b1b !important;
    }
    .persistent-dismissible-notice.notice-error .notice-content,
    .persistent-dismissible-notice.notice-error .notice-message {
        color: #991b1b !important;
    }
    .persistent-dismissible-notice.notice-error .notice-close-btn {
        color: #b91c1c !important;
    }
    .persistent-dismissible-notice.notice-error .notice-close-btn:hover {
        color: #7f1d1d !important;
        background-color: rgba(220, 38, 38, 0.15) !important;
    }

    @media (prefers-color-scheme: dark) {
        .persistent-dismissible-notice {
            background-color: rgba(6, 78, 59, 0.35) !important;
            border-color: rgba(52, 211, 153, 0.35) !important;
            border-left-color: #34d399 !important;
            color: #ecfdf5 !important;
        }
        .persistent-dismissible-notice .notice-content,
        .persistent-dismissible-notice .notice-message {
            color: #ecfdf5 !important;
        }
        .persistent-dismissible-notice .notice-close-btn {
            color: #86efac !important;
        }
        .persistent-dismissible-notice .notice-close-btn:hover {
            color: #ffffff !important;
            background-color: rgba(255, 255, 255, 0.15) !important;
        }
        .persistent-dismissible-notice.notice-warning {
            background-color: rgba(120, 53, 15, 0.42) !important;
            border-color: rgba(251, 191, 36, 0.45) !important;
            border-left-color: #fbbf24 !important;
            color: #fef3c7 !important;
        }
        .persistent-dismissible-notice.notice-warning .notice-content,
        .persistent-dismissible-notice.notice-warning .notice-message {
            color: #fef3c7 !important;
        }
        .persistent-dismissible-notice.notice-warning .notice-close-btn {
            color: #fde68a !important;
        }
        .persistent-dismissible-notice.notice-error {
            background-color: rgba(127, 29, 29, 0.45) !important;
            border-color: rgba(248, 113, 113, 0.45) !important;
            border-left-color: #f87171 !important;
            color: #fee2e2 !important;
        }
        .persistent-dismissible-notice.notice-error .notice-content,
        .persistent-dismissible-notice.notice-error .notice-message {
            color: #fee2e2 !important;
        }
        .persistent-dismissible-notice.notice-error .notice-close-btn {
            color: #fca5a5 !important;
        }
    }
</style>
""", unsafe_allow_html=True)

    # Centered Action Loading Overlay DOM & Client Interceptor
    st.html("""
    <div id="news-bot-loading-overlay">
        <div class="action-spinner-circle"></div>
        <div id="news-bot-loading-title" class="action-spinner-title">Aktion wird ausgeführt...</div>
        <div id="news-bot-loading-subtitle" class="action-spinner-subtitle">Bitte einen Moment Geduld</div>
    </div>
    <script>
    (function() {
        function getOverlay(doc) {
            var el = doc.getElementById("news-bot-loading-overlay");
            if (!el && window.parent && window.parent.document) {
                try { el = window.parent.document.getElementById("news-bot-loading-overlay"); } catch(e) {}
            }
            return el;
        }

        function hideActionOverlay() {
            var overlay = getOverlay(document);
            if (overlay) {
                overlay.classList.remove("active");
                overlay.style.display = "none";
                overlay.style.opacity = "0";
                overlay.style.visibility = "hidden";
                overlay.style.pointerEvents = "none";
            }
        }
        window._newsBotHideActionOverlay = hideActionOverlay;

        function showActionOverlay(title, subtitle) {
            var overlay = getOverlay(document);
            if (overlay) {
                var titleEl = overlay.querySelector(".action-spinner-title") || document.getElementById("news-bot-loading-title");
                var subEl = overlay.querySelector(".action-spinner-subtitle") || document.getElementById("news-bot-loading-subtitle");
                if (titleEl && title) titleEl.innerText = title;
                if (subEl && subtitle) subEl.innerText = subtitle;
                overlay.classList.add("active");
                overlay.style.display = "flex";
                overlay.style.opacity = "1";
                overlay.style.visibility = "visible";
                overlay.style.pointerEvents = "all";

                overlay.onclick = function() {
                    hideActionOverlay();
                };
            }

            // Watch for Streamlit execution finish and release overlay
            var startTime = Date.now();
            var wasRunning = false;
            var checkIdleInterval = setInterval(function() {
                var doc = (overlay && overlay.ownerDocument) ? overlay.ownerDocument : document;
                var rootApp = doc.querySelector('.stApp') || (window.parent && window.parent.document ? window.parent.document.querySelector('.stApp') : null);
                var runningIcon = doc.querySelector('[data-testid="stStatusWidgetRunningIcon"]') || (window.parent && window.parent.document ? window.parent.document.querySelector('[data-testid="stStatusWidgetRunningIcon"]') : null);
                
                var isRunning = false;
                if (runningIcon) {
                    isRunning = true;
                } else if (rootApp && rootApp.getAttribute('data-test-script-state') === 'running') {
                    isRunning = true;
                }

                if (isRunning) {
                    wasRunning = true;
                }

                var elapsed = Date.now() - startTime;
                if (wasRunning && !isRunning && elapsed > 400) {
                    clearInterval(checkIdleInterval);
                    hideActionOverlay();
                }
            }, 200);

            // Sicherheits-Timeout (Fallback): Mindestens 35 Sekunden für längere KI-Generierungen und Feed-Aktualisierungen
            setTimeout(function() {
                clearInterval(checkIdleInterval);
                hideActionOverlay();
            }, 35000);
        }
        window._newsBotShowActionOverlay = showActionOverlay;

        // Auto-hide immediately upon any script execution / re-render
        hideActionOverlay();
        setTimeout(hideActionOverlay, 80);
        setTimeout(hideActionOverlay, 300);
        setTimeout(hideActionOverlay, 800);

        // Escape key fallback
        document.addEventListener("keydown", function(e) {
            if (e.key === "Escape" || e.keyCode === 27) {
                hideActionOverlay();
            }
        });

        function handleClick(e) {
            var btn = e.target && e.target.closest ? e.target.closest("button") : null;
            if (!btn) return;
            if (btn.disabled || btn.classList.contains("disabled-nav-btn") || btn.classList.contains("arch-html-btn") || btn.classList.contains("arch-open-tab-btn") || btn.classList.contains("arch-art-dl-btn")) return;
            var keyHolder = btn.closest("[class*='st-key-']");
            var keyClass = keyHolder ? keyHolder.className : "";
            if (!keyClass) return;

            var overlayActions = [
                ["btn_add_cat", "📁 Speichere Kategorie...", "Schreibe Änderung direkt in die Datenbank..."],
                ["btn_ren_cat", "✏️ Benenne Kategorie um...", "Speichere Änderung direkt in der Datenbank..."],
                ["del_cat_", "🗑️ Lösche Kategorie...", "Entferne Kategorie und speichere in der Datenbank..."],
                ["btn_submit_new_feed", "➕ Speichere neuen Feed...", "Speichere in der Datenbank & lade Artikel..."],
                ["btn_save_feed_", "💾 Speichere Feed...", "Aktualisiere Feed direkt in der Datenbank..."],
                ["del_feed_", "🗑️ Lösche Feed...", "Entferne Feed & bereinige Datenbank..."],
                ["btn_apply_settings", "⚙️ Speichere Einstellungen...", "Speichere direkt in der Datenbank..."],
                ["btn_save_ki_prompts", "💾 Speichere KI-Prompts...", "Speichere Prompts direkt in der Datenbank..."],
                ["btn_reset_ki_main_prompt", "↩️ Setze Hauptprompt zurück...", "Speichere Standard-Prompt in der Datenbank..."],
                ["btn_save_ai_brain_policy", "🧠 Speichere AI-Brain Optionen...", "Aktualisiere AI-Brain Policy..."],
                ["btn_generate_briefing", "🧠 Generiere KI-Briefing...", "Analysiere Artikel und erstelle Zusammenfassung..."],
                ["sb_btn_refresh_feeds", "🔄 Lese RSS-Feeds ein...", "Lade Feeds live und aktualisiere Datenbank..."],
                ["btn_refresh_rss", "📡 Generiere RSS-Feeds neu...", "Erstelle XML-Dateien für Feedly & Reader..."],
                ["btn_push_rss_cdn", "🚀 Pushe Feeds zu GitHub & CDN...", "Synchronisiere RSS-Feeds mit GitHub & CDN..."],
                ["btn_archive_sync_refresh", "🔄 Aktualisiere Offline-Archiv...", "Synchronisiere Offline-Feeds und lade gespeicherte Artikel..."],
                ["arch_toggle_off_", "⚙️ Speichere Offline-Feed...", "Aktualisiere Feed-Einstellung in der Datenbank..."],
                ["btn_restore_sb_", "🔄 Stelle Settings-Backup wieder her...", "Speichere Einstellungen direkt in der Datenbank..."],
                ["btn_confirm_del_sb_", "🗑️ Lösche Settings-Backup...", "Entferne Backup aus der Datenbank..."],
                ["btn_archive_cleanup_90d", "🧹 Bereinige Offline-Archiv...", "Entferne abgelaufene Artikel älter als 90 Tage..."]
            ];

            for (var i = 0; i < overlayActions.length; i++) {
                if (keyClass.indexOf(overlayActions[i][0]) !== -1) {
                    showActionOverlay(overlayActions[i][1], overlayActions[i][2]);
                    return;
                }
            }
        }

        function handleNoticeClose(e) {
            var target = e.target;
            if (!target) return;
            var closeBtn = target.closest ? target.closest(".notice-close-btn") : null;
            if (!closeBtn && target.classList && target.classList.contains("notice-close-btn")) {
                closeBtn = target;
            }
            if (closeBtn) {
                var notice = closeBtn.closest ? closeBtn.closest(".persistent-dismissible-notice") : null;
                if (!notice && closeBtn.parentElement) {
                    notice = closeBtn.parentElement.closest(".persistent-dismissible-notice");
                }
                if (notice) {
                    var bannerId = notice.getAttribute("id") || notice.getAttribute("data-banner-id");
                    if (bannerId) {
                        try { sessionStorage.setItem(bannerId, "dismissed"); } catch(err) {}
                    }
                    notice.style.setProperty("display", "none", "important");
                    notice.remove();
                }
            }
        }

        document.addEventListener("click", handleNoticeClose, true);
        document.addEventListener("click", handleClick, true);
        try {
            if (window.parent && window.parent.document && window.parent.document !== document) {
                window.parent.document.addEventListener("click", handleNoticeClose, true);
                window.parent.document.addEventListener("click", handleClick, true);
            }
        } catch(e) {}
    })();
    </script>
    """, unsafe_allow_javascript=True)
