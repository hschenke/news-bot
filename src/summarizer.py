from __future__ import annotations

import logging
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal, overload

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    import ai_brain
    from ai_brain import (
        AppBrainPolicy,
        BrainAuthError,
        BrainError,
        BrainProviderError,
        BrainTier,
        default_app_policy,
    )
except ImportError:  # pragma: no cover - minimal CI environments without bot-dashboard
    ai_brain = None  # type: ignore[assignment]
    AppBrainPolicy = Any  # type: ignore[assignment,misc]
    BrainTier = Any  # type: ignore[assignment,misc]

    class BrainError(Exception):  # type: ignore[no-redef]
        """Base exception when ai_brain package is unavailable."""

    class BrainAuthError(BrainError):  # type: ignore[no-redef]
        """Auth exception when ai_brain package is unavailable."""

    class BrainProviderError(BrainError):  # type: ignore[no-redef]
        """Provider exception when ai_brain package is unavailable."""

    def default_app_policy(app_name: str = "") -> Any:  # type: ignore[misc]
        return None

logger = logging.getLogger(__name__)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SYSTEM_INSTRUCTION: str = (
    "Du bist ein professioneller News-Kurator und Redakteur für ein Daily News Briefing. "
    "Erstelle faktenbasierte, kompakte Markdown-Briefings ohne Executive Summary, ohne Infoboxen "
    "und mit exakt unveränderten Original-URLs."
)


@dataclass(frozen=True)
class BriefingResult:
    """Strukturiertes Ergebnis einer KI-Briefing-Generierung über `ai_brain` inkl. Modell- & Tier-Metadaten."""

    markdown: str
    model_used: str
    tier_used: str = ""
    backend: str = "ai_brain"

    @property
    def display_label(self) -> str:
        """Formatiert eine kompakte Modell-/Tier-Anzeige für Logs, Datenbank und UI."""
        tier_label = self.tier_used.capitalize() if self.tier_used else "Auto"
        model_label = self.model_used or "AI-Brain"
        return f"{model_label} · AI-Brain ({tier_label})"


_last_briefing_result: BriefingResult | None = None


def get_last_briefing_result() -> BriefingResult | None:
    """Liefert das zuletzt erzeugte BriefingResult inkl. genutztem Modell und Tier."""
    return _last_briefing_result


def is_ai_brain_configured() -> bool:
    """Prüft ohne Netzwerkaufruf, ob ein gültiger `AI_BRAIN_KEY` konfiguriert ist."""
    if ai_brain is None:
        return False
    return bool(ai_brain.is_configured())


def load_ai_brain_policy() -> tuple[AppBrainPolicy, str | None]:
    """Lädt die gespeicherte `AppBrainPolicy` für News-Bot oder liefert Free-Only-Defaults."""
    default_policy = default_app_policy("news-bot")
    if not is_ai_brain_configured():
        return default_policy, None
    try:
        with ai_brain.connect() as brain:
            return brain.get_app_policy(), None
    except BrainError as exc:
        logger.warning(
            "AI-Brain Policy konnte nicht geladen werden (%s). Nutze News-Bot Defaults.",
            exc,
        )
        return default_policy, str(exc)


def save_ai_brain_policy(
    llm_tier: BrainTier | str,
    search_tier: BrainTier | str,
    allow_paid: bool = False,
) -> AppBrainPolicy:
    """Validiert und speichert die `AppBrainPolicy` zentral via `ai_brain` (maps_mode='off')."""
    if ai_brain is None or not ai_brain.is_configured():
        raise BrainAuthError("AI-Brain ist nicht konfiguriert (AI_BRAIN_KEY fehlt).")

    resolved_llm = (
        llm_tier if isinstance(llm_tier, BrainTier) else BrainTier(str(llm_tier).strip().lower())
    )
    resolved_search = (
        search_tier
        if isinstance(search_tier, BrainTier)
        else BrainTier(str(search_tier).strip().lower())
    )
    policy = AppBrainPolicy(
        llm_tier=resolved_llm,
        search_tier=resolved_search,
        allow_paid=bool(allow_paid),
        maps_mode="off",
    )
    with ai_brain.connect() as brain:
        saved = brain.set_app_policy(policy)

    logger.info(
        "AI-Brain Policy gespeichert: llm_tier=%s, search_tier=%s, allow_paid=%s, maps_mode=%s",
        saved.llm_tier.value,
        saved.search_tier.value,
        saved.allow_paid,
        saved.maps_mode,
    )
    return saved


def get_streamlit_app_url(*_args: Any, **_kwargs: Any) -> str:
    """Ermittelt die Basis-URL der Streamlit Web-App ausschließlich über die Umgebungsvariable STREAMLIT_APP_URL."""
    env_url = os.getenv("STREAMLIT_APP_URL")
    if env_url and env_url.strip():
        return env_url.strip().rstrip("/")

    if "STREAMLIT_SERVER_PORT" in os.environ or "streamlit" in sys.modules:
        try:
            import streamlit as st

            if hasattr(st, "secrets") and "STREAMLIT_APP_URL" in st.secrets:
                return str(st.secrets["STREAMLIT_APP_URL"]).strip().rstrip("/")
        except Exception as exc:  # noqa: BLE001 - Streamlit Secrets sind optional
            logger.debug("Streamlit Secret STREAMLIT_APP_URL nicht lesbar: %s", type(exc).__name__)

    return ""


def build_category_quicklinks(
    config: dict[str, Any],
    streamlit_base_url: str,
    *args: Any,
    **kwargs: Any,
) -> dict[str, str]:
    """
    Erstellt für jede Kategorie den Link-Block.
    WICHTIG: Die Feed-Links verweisen direkt auf die Streamlit-App (mit Kategorie- & Feed-Filter),
    damit der Nutzer die formatierten Artikel sieht und nicht die unlesbare Roh-RSS-XML.
    Falls ein APP_PASSWORD hinterlegt ist, wird das sichere Auth-Token an die URLs angehängt,
    sodass der Nutzer bei Klicks aus dem Briefing nie wieder ein Passwort eingeben muss!
    """
    clean_base_url = (streamlit_base_url or "").strip().rstrip("/")
    try:
        from src.auth import generate_readonly_auth_token, get_configured_app_password
    except ImportError:
        import importlib

        import src.auth
        importlib.reload(src.auth)
        from src.auth import generate_readonly_auth_token, get_configured_app_password
    app_pw = get_configured_app_password()
    auth_param = f"&auth={generate_readonly_auth_token(app_pw)}" if app_pw else ""

    category_links: dict[str, str] = {}
    for cat_item in config.get("categories", []):
        cat_name = cat_item.get("name", "").strip()
        if not cat_name:
            continue
        if not clean_base_url:
            category_links[cat_name] = ""
            continue
        feeds = cat_item.get("feeds", [])
        encoded_cat = urllib.parse.quote(cat_name)
        cat_app_url = f"{clean_base_url}/?category={encoded_cat}{auth_param}"

        feed_parts = []
        for f in feeds:
            fname = f.get("name", "Feed").strip()
            encoded_feed = urllib.parse.quote(fname)
            feed_app_url = f"{clean_base_url}/?category={encoded_cat}&feed={encoded_feed}{auth_param}"
            feed_parts.append(f"[{fname}]({feed_app_url})")

        if len(feed_parts) == 1:
            feed_str = f"Feed: {feed_parts[0]}"
        elif len(feed_parts) > 1:
            feed_str = f"Feeds: {', '.join(feed_parts)}"
        else:
            feed_str = ""

        if feed_str:
            category_links[cat_name] = f"> 🔗 [Streamlit App]({cat_app_url}) · {feed_str}"
        else:
            category_links[cat_name] = f"> 🔗 [Streamlit App]({cat_app_url})"

    return category_links


DEFAULT_MAIN_PROMPT_TEMPLATE = """Du bist ein professioneller News-Kurator und Redakteur für ein Daily News Briefing.
Deine Aufgabe ist es, aus den folgenden Roh-Nachrichten ein übersichtliches, kompaktes und faktenbasiertes Tages-Briefing auf {lang_name} zu erstellen.

WICHTIGE FORMATIERUNGSRICHTLINIEN:
1. KEINE ZUSAMMENFASSUNGEN:
   - KEIN "Executive Summary", KEINE Einleitung und KEINE vorangestellte Gesamtzusammenfassung!
   - KEINE Infobox und KEINE zusammenfassende Kompakt-Box pro Kategorie (KEIN '> 💡 Kompakt:' o. ä.)!
   - Starte direkt mit der ersten Kategorie (z. B. "## 🤖 Tech & AI").
2. STRUKTUR PRO KATEGORIE:
   - Jede Kategorie beginnt mit ihrer Überschrift (z. B. "## 🤖 Tech & AI").
   - Setze direkt unter die Kategorie-Überschrift exakt die vorgegebene Quicklinks-Zeile (Links zur Streamlit App & passenden Feeds) ein.
   - Direkt unter der Quicklinks-Zeile folgen als Aufzählung die bis zu 5 wichtigsten und relevantesten Einträge dieser Kategorie.
3. INHALT DER EINTRÄGE (KEIN SCHWURBELN, ECHTE FAKTEN):
   - Verlinke den Titel jedes Eintrags direkt mit der Originalquelle als Markdown-Link.
   - WICHTIG ZU DEN LINKS:
     - Übernimm die Original-URL aus dem Feld 'Link:' immer exakt 1:1 unverändert als Ziel des Markdown-Links.
     - Kürze niemals die URL und hänge NIEMALS Bindestriche (-) oder sonstige Satzzeichen an das Ende der URL an!
   - Nicht selber etwas dazuerfinden, spekulieren, interpretieren oder zusammenschwurbeln!
   - Keine werblichen Floskeln, kein Marketing-Sprech und keine Meta-Einleitungen (wie z. B. "Dieser Beitrag thematisiert...", "Spannende Einblicke in...").
   - Gib stattdessen den tatsächlichen sachlichen Inhalt des Artikels kurz und prägnant wieder: nenne die konkreten Fakten, Ereignisse, Zahlen oder Kernaussagen aus dem Inhalt in 1 bis maximal 2 klaren, präzisen Sätzen.
   - Das Wort "TL;DR:" NICHT verwenden!
   - Format für normale Nachrichtenartikel:
     - **[Artikeltitel](Original-URL)**: <Tatsächlicher sachlicher Inhalt kurz und prägnant in 1-2 Sätzen>
4. SPEZIALFÄLLE (WITZ / HUMOR / QUOTES):
   - Spezialfälle wie Witze, Pointen, Cartoons oder Aphorismen (z. B. in der Kategorie Fun oder beim Witz des Tages) dürfen NIEMALS zusammengefasst, umschrieben oder erklärt werden!
   - Stelle bei solchen Spezialfällen einfach den KOMPLETTEN Witz als vollständigen Text dar!
   - Format für Witze:
     - **[Artikeltitel](Original-URL)**:
     <Vollständiger Witz-Text inklusive aller Dialoge und Pointe>
5. AUSWAHL & NUTZER-FEEDBACK:
   - Wähle pro Kategorie maximal die TOP 5 wichtigsten Einträge aus.
   - Bevorzuge unbedingt Artikel, die vom Nutzer positiv bewertet / geliked wurden (mit '⭐ [NUTZER-FAVORIT / GELIKED]' markiert).
   - Artikel, die mit '⚠️ [VOM NUTZER ALS WENIGER RELEVANT / GEDISLIKED]' gekennzeichnet sind, sollen nach Möglichkeit ignoriert oder nur nachrangig berücksichtigt werden.
6. Verwende sauberes Markdown mit gut strukturierten Zwischenüberschriften (##) und passenden Emojis.
7. REDAKTIONELLE FILTER & ANWEISUNGEN:
{active_directives}

Hier sind die aktuellen Roh-Nachrichten nach Kategorien gegliedert:
{context_data}
"""

DEFAULT_DIRECTIVES = """- Filtere reine Werbung, Angebote, Sonderaktionen, Rabatte, Advertorials oder gesponserte Beiträge strikt heraus.
- Nimm nur Artikel auf, die einen echten nachrichtlichen Informationswert bieten."""


def _strip_executive_summaries(text: str) -> str:
    """Entfernt Executive Summary Abschnitte und globale Überschriften."""
    cleaned = re.sub(
        r"(?is)^#{1,3}\s*(?:📌\s*)?Executive Summary.*?(?=\n#{1,3}\s+[^\n]+|\Z)",
        "",
        text,
    )
    cleaned = re.sub(
        r"(?i)^#{1,2}\s+(?:📊\s*)?(?:Daily\s+)?(?:Executive\s+)?Briefing[^\n]*\n+",
        "",
        cleaned.strip(),
    )
    return re.sub(r"^(?:\s*---\s*\n+)+", "", cleaned).strip()


def _strip_tldr_markers(text: str) -> str:
    """Entfernt redundante TL;DR Kennzeichnungen aus Aufzählungspunkten."""
    text = re.sub(r"(?<=\*\*:\s)(?:TL;?DR:?\s*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\]\):\s)(?:TL;?DR:?\s*)", "", text, flags=re.IGNORECASE)
    return re.sub(r"\bTL;?DR:?\s*", "", text, flags=re.IGNORECASE)


def _match_category_name(raw_title: str, available_categories: list[str]) -> str | None:
    """Findet die passende Kategorie anhand von Normalisierung und Substring-Matching."""
    cleaned_title = re.sub(r"[^\w\s&]", "", raw_title).strip().lower()
    for cat_name in available_categories:
        c_clean = re.sub(r"[^\w\s&]", "", cat_name).strip().lower()
        if c_clean and (c_clean in cleaned_title or cleaned_title in c_clean):
            return cat_name
    return None


def _sanitize_briefing_urls(
    text: str,
    known_urls_map: dict[str, str] | None = None,
) -> str:
    """
    Bereinigt alle Markdown-Links in einem Text:
    - Entfernt störende Bindestriche (-) oder Satzzeichen am Ende der URLs.
    - Gleicht die URL gegen bekannte Original-URLs aus den Feeds ab, falls vorhanden.
    """
    from src.filters import sanitize_markdown_url

    def _replace_link(match: re.Match) -> str:
        title = match.group(1)
        raw_url = match.group(2)
        cleaned_url = sanitize_markdown_url(raw_url)
        if known_urls_map:
            canonical = (
                known_urls_map.get(cleaned_url)
                or known_urls_map.get(cleaned_url.rstrip("/"))
                or known_urls_map.get(cleaned_url.rstrip("-/"))
            )
            if canonical:
                cleaned_url = canonical
        return f"[{title}]({cleaned_url})"

    return re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", _replace_link, text)


def _clean_and_enhance_briefing(
    text: str,
    category_links: dict[str, str],
    max_items_per_category: int = 5,
    known_urls_map: dict[str, str] | None = None,
) -> str:
    """
    Bereinigt das KI-Briefing:
    - Entfernt jegliches Executive Summary oder einleitende Vorab-Zusammenfassungen
    - Entfernt jegliche Infoboxen oder Kompakt-Zusammenfassungs-Boxen (> 💡 Kompakt: etc.)
    - Entfernt das Wort 'TL;DR:' vor den Einträgen
    - Bereinigt alle Markdown-Links (entfernt nachgestellte Bindestriche/Satzzeichen und gleicht mit Original-URLs ab)
    - Platziert direkt unter jeder Kategorie die Quicklinks-Zeile zur Streamlit-App & Feeds
    - Begrenzt die Artikelanzahl pro Kategorie strikt auf maximal TOP 5
    - Unterstützt vollständige Mehrzeilen-Texte für Spezialfälle (z. B. Witze & Dialoge)
    """
    text = _strip_executive_summaries(text)
    text = _strip_tldr_markers(text)
    text = _sanitize_briefing_urls(text, known_urls_map)

    lines = text.split("\n")
    processed_lines: list[str] = []
    current_cat: str | None = None
    cat_links_inserted = False
    cat_item_count = 0

    header_pattern = re.compile(r"^(#{2,3})\s+(.*)$")
    available_cats = list(category_links.keys())

    def flush_quicklinks_if_needed():
        nonlocal cat_links_inserted
        if current_cat and not cat_links_inserted:
            ql = category_links.get(current_cat)
            if ql:
                processed_lines.append("")
                processed_lines.append(ql)
                processed_lines.append("")
            cat_links_inserted = True

    for line in lines:
        match = header_pattern.match(line.strip())
        if match:
            flush_quicklinks_if_needed()
            raw_title = match.group(2).strip()
            current_cat = _match_category_name(raw_title, available_cats)
            cat_links_inserted = False
            cat_item_count = 0
            processed_lines.append(line)
            continue

        stripped = line.strip()

        # Erkennung von Quicklinks
        if current_cat and "Streamlit App" in stripped:
            if not cat_links_inserted:
                clean_line = stripped if stripped.startswith(">") else f"> {stripped}"
                processed_lines.append(clean_line)
                processed_lines.append("")
                cat_links_inserted = True
            continue

        # Entfernung jeglicher Infoboxen / Kompakt-Boxen (> ...) vor den Artikeln
        if current_cat and cat_item_count == 0 and stripped.startswith(">"):
            continue

        # Erkennung von Artikellistenpunkten (- oder *)
        if current_cat and stripped.startswith(("- ", "* ")):
            flush_quicklinks_if_needed()
            cat_item_count += 1
            if cat_item_count <= max_items_per_category:
                processed_lines.append(line)
            continue

        # Fortsetzungszeilen von Artikeln (z.B. eingerückte Zeilen oder Witz-Textzeilen)
        if current_cat and cat_item_count > 0:
            if cat_item_count <= max_items_per_category:
                processed_lines.append(line)
            continue

        if not current_cat:
            processed_lines.append(line)

    flush_quicklinks_if_needed()

    result = "\n".join(processed_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _briefing_sort_key(item: dict[str, Any]) -> tuple[int, float]:
    """Sortierschlüssel: Like (2) > Neutral (1) > Dislike (0), sekundär nach Zeitstempel."""
    fb = int(item.get("feedback") or 0)
    priority = 2 if fb > 0 else (0 if fb < 0 else 1)
    ts = float(item.get("timestamp") or 0.0)
    return (priority, ts)


def _build_briefing_prompt_and_urls(
    categorized_news: dict[str, list[dict[str, Any]]],
    category_links: dict[str, str],
    lang_name: str,
    active_main_template: str,
    active_directives: str,
) -> tuple[str, dict[str, str]]:
    """Erstellt den vollständigen Briefing-Prompt sowie die Canonical-URL-Zuordnungstabelle."""
    context_lines: list[str] = []
    known_urls_map: dict[str, str] = {}

    for cat, items in sorted(categorized_news.items(), key=lambda x: x[0].strip().lower()):
        if not items:
            continue
        context_lines.append(f"\n\n## {cat}")
        quicklink = category_links.get(cat, "")
        if quicklink:
            context_lines.append(f"\nQuicklinks-Zeile für '{cat}':\n{quicklink}\n")

        sorted_items = sorted(items, key=_briefing_sort_key, reverse=True)
        is_cat_humor = any(term in cat.lower() for term in ("fun", "witz", "humor", "spass", "spaß"))
        for item in sorted_items:
            fb = int(item.get("feedback") or 0)
            fav_tag = " ⭐ [NUTZER-FAVORIT / GELIKED - BITTE BEVORZUGT BERÜCKSICHTIGEN]" if fb > 0 else ""
            dislike_tag = " ⚠️ [VOM NUTZER ALS WENIGER RELEVANT / GEDISLIKED MARKIERT]" if fb < 0 else ""
            source_name = item.get("source", "Unbekannt")
            source_url = item.get("source_url", "")
            is_item_joke = (
                is_cat_humor
                or any(term in str(source_name).lower() for term in ("witz", "joke", "debeste", "lach", "cartoon"))
                or any(term in str(source_url).lower() for term in ("witz", "joke", "debeste"))
                or any(term in str(item.get("title", "")).lower() for term in ("witz", "joke"))
            )
            joke_tag = (
                " 🎭 [SPEZIALFALL: WITZ / HUMOR - DEN KOMPLETTEN WITZ VOLLSTÄNDIG ALS TEXT DARSTELLEN! "
                "KEINE ZUSAMMENFASSUNG, NICHT ERKLÄREN, NICHT KÜRZEN!]"
                if is_item_joke
                else ""
            )
            context_lines.append(
                f"- **{item['title']}**{fav_tag}{dislike_tag}{joke_tag} (Quelle: {source_name})"
            )
            if item.get("summary"):
                label = "Kompletter Text / Pointe" if is_item_joke else "Tatsächlicher Inhalt"
                context_lines.append(f"  {label}: {item['summary']}")
            context_lines.append(f"  Link: {item['link']}")

            it_link = (item.get("link") or "").strip()
            if it_link:
                known_urls_map[it_link] = it_link
                known_urls_map[it_link.rstrip("/")] = it_link
                known_urls_map[it_link.rstrip("-/")] = it_link

    context_data_str = "".join(context_lines)

    prompt = active_main_template
    if "{lang_name}" in prompt:
        prompt = prompt.replace("{lang_name}", lang_name)
    if "{active_directives}" in prompt:
        prompt = prompt.replace("{active_directives}", active_directives)
    else:
        prompt += f"\n\nREDAKTIONELLE FILTER & ANWEISUNGEN:\n{active_directives}\n"
    if "{context_data}" in prompt:
        prompt = prompt.replace("{context_data}", context_data_str)
    else:
        prompt += f"\n\nHier sind die aktuellen Roh-Nachrichten nach Kategorien gegliedert:\n{context_data_str}\n"

    return prompt, known_urls_map


def _generate_via_ai_brain(
    prompt: str,
    system_instruction: str,
) -> tuple[str, str, str]:
    """
    Führt die Briefing-Synthese ausschließlich über das zentrale `ai_brain`-Framework (`tier="auto"`) aus.
    Gibt bei Erfolg `(text, model_used, tier_used)` zurück; wirft bei fehlendem Key oder Fehler `BrainError`.
    """
    if ai_brain is None or not ai_brain.is_configured():
        raise BrainAuthError("AI-Brain ist nicht konfiguriert (AI_BRAIN_KEY fehlt).")

    try:
        with ai_brain.connect() as brain:
            result = brain.generate_text(
                prompt,
                tier="auto",
                system_instruction=system_instruction,
                allow_paid=None,
            )
    except BrainError:
        logger.exception("AI-Brain Briefing-Aufruf fehlgeschlagen.")
        raise

    raw_text = (getattr(result, "text", "") or "").strip()
    if not raw_text:
        raise BrainProviderError("AI-Brain lieferte eine leere Antwort.")

    model_used = str(getattr(result, "model_used", "") or "ai_brain").strip()
    raw_tier = getattr(result, "tier", "")
    tier_used = raw_tier.value if hasattr(raw_tier, "value") else str(raw_tier or "auto").strip()
    logger.info(
        "AI-Brain Briefing erfolgreich generiert (Modell: %s, Tier: %s, %d Zeichen).",
        model_used,
        tier_used,
        len(raw_text),
    )
    return raw_text, model_used, tier_used


def generate_news_briefing(
    categorized_news: dict[str, list[dict[str, Any]]],
    config_path: str = "config/sources.yaml",
    main_prompt_template: str | None = None,
    custom_directives: str | None = None,
    system_instruction: str | None = None,
) -> BriefingResult:
    """
    Erzeugt das kuratierte Daily News Briefing ausschließlich über das zentrale `ai_brain` (`tier="auto"`)
    und gibt ein strukturiertes `BriefingResult` inklusive Modellnamen und Tier zurück.
    """
    global _last_briefing_result

    try:
        from src.sources_manager import load_prompts, load_settings, load_sources_raw

        config = load_sources_raw(config_path)
        settings = load_settings()
        prompts = load_prompts()
    except Exception as exc:  # noqa: BLE001 - fehlende YAML-Dateien nutzen Standard-Defaults
        logger.warning("Konfiguration konnte nicht geladen werden (%s). Verwende Defaults.", exc)
        config = {"categories": []}
        settings = {}
        prompts = {}

    language = settings.get("language", "de")
    lang_name = "Deutsch" if language == "de" else language

    streamlit_app_url = get_streamlit_app_url()
    category_links = build_category_quicklinks(config, streamlit_app_url)

    active_directives = (custom_directives or "").strip()
    if not active_directives:
        active_directives = (
            prompts.get("custom_prompt_directives") or settings.get("custom_prompt_directives") or ""
        ).strip()
    if not active_directives:
        active_directives = DEFAULT_DIRECTIVES

    active_main_template = (main_prompt_template or "").strip()
    if not active_main_template:
        active_main_template = (
            prompts.get("custom_main_prompt") or settings.get("custom_main_prompt") or ""
        ).strip()
    if not active_main_template:
        active_main_template = DEFAULT_MAIN_PROMPT_TEMPLATE

    active_system_instruction = (system_instruction or "").strip() or DEFAULT_SYSTEM_INSTRUCTION
    prompt, known_urls_map = _build_briefing_prompt_and_urls(
        categorized_news=categorized_news,
        category_links=category_links,
        lang_name=lang_name,
        active_main_template=active_main_template,
        active_directives=active_directives,
    )

    raw_text, model_used, tier_used = _generate_via_ai_brain(
        prompt,
        system_instruction=active_system_instruction,
    )
    cleaned_result = _clean_and_enhance_briefing(
        raw_text,
        category_links,
        known_urls_map=known_urls_map,
    )
    res = BriefingResult(
        markdown=cleaned_result,
        model_used=model_used,
        tier_used=tier_used,
        backend="ai_brain",
    )
    _last_briefing_result = res
    return res


@overload
def summarize_news_with_gemini(
    categorized_news: dict[str, list[dict[str, Any]]],
    config_path: str = "config/sources.yaml",
    main_prompt_template: str | None = None,
    custom_directives: str | None = None,
    *,
    system_instruction: str | None = None,
    return_result: Literal[False] = False,
) -> str: ...


@overload
def summarize_news_with_gemini(
    categorized_news: dict[str, list[dict[str, Any]]],
    config_path: str = "config/sources.yaml",
    main_prompt_template: str | None = None,
    custom_directives: str | None = None,
    *,
    system_instruction: str | None = None,
    return_result: Literal[True],
) -> BriefingResult: ...


def summarize_news_with_gemini(
    categorized_news: dict[str, list[dict[str, Any]]],
    config_path: str = "config/sources.yaml",
    main_prompt_template: str | None = None,
    custom_directives: str | None = None,
    *,
    system_instruction: str | None = None,
    return_result: bool = False,
) -> str | BriefingResult:
    """
    Fasst die gesammelten Nachrichten ausschließlich über `ai_brain` (`tier="auto"`) zusammen.

    Mit `return_result=True` wird das strukturierte `BriefingResult` inklusive
    genutztem Modellnamen und Tier zurückgegeben; Standard (`False`) gibt `str` zurück.
    """
    result = generate_news_briefing(
        categorized_news=categorized_news,
        config_path=config_path,
        main_prompt_template=main_prompt_template,
        custom_directives=custom_directives,
        system_instruction=system_instruction,
    )
    return result if return_result else result.markdown

