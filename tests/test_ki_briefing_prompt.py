import unittest

from src.notifier import markdown_to_html_email
from src.summarizer import (
    DEFAULT_DIRECTIVES,
    DEFAULT_MAIN_PROMPT_TEMPLATE,
    _build_briefing_prompt_and_urls,
    _clean_and_enhance_briefing,
)


class TestKiBriefingPrompt(unittest.TestCase):
    def test_clean_and_enhance_briefing_removes_summary_and_caps_at_top_5(self):
        text = """## 🤖 Tech & AI
> 💡 **Kompakt:** Die wichtigsten News zusammengefasst in zwei Sätzen.
- **[A](http://a)**: Beschreibung 1
- **[B](http://b)**: Beschreibung 2
- **[C](http://c)**: Beschreibung 3
- **[D](http://d)**: Beschreibung 4
- **[E](http://e)**: Beschreibung 5
- **[F](http://f)**: Beschreibung 6
- **[G](http://g)**: Beschreibung 7
"""
        cat_links = {"Tech & AI": "> 🔗 [Streamlit App](http://app)"}
        result = _clean_and_enhance_briefing(text, cat_links)

        self.assertIn("## 🤖 Tech & AI", result)
        # Keine Zusammenfassungs-Infobox mehr!
        self.assertNotIn("💡 **Kompakt:**", result)
        self.assertIn("Streamlit App", result)
        self.assertIn("[A](http://a)", result)
        self.assertIn("[E](http://e)", result)
        # Artikel über Top 5 hinaus werden ausgeschlossen
        self.assertNotIn("[F](http://f)", result)
        self.assertNotIn("[G](http://g)", result)

    def test_clean_and_enhance_briefing_preserves_multiline_joke(self):
        text = """## 😂 Fun
> 🔗 [Streamlit App](http://app)
- **[Witz des Tages](http://joke)**:
Treffen sich zwei Blondinen.
"Ich hatte gestern Führerscheinprüfung und bin durchgefallen."
"Warum denn das?"
"Naja, ich bin Landstraße gefahren..."
"""
        cat_links = {"Fun": "> 🔗 [Streamlit App](http://app)"}
        result = _clean_and_enhance_briefing(text, cat_links)

        self.assertIn("## 😂 Fun", result)
        self.assertIn("[Witz des Tages](http://joke)", result)
        self.assertIn("Treffen sich zwei Blondinen.", result)
        self.assertIn("Führerscheinprüfung", result)

    def test_prompt_builder_marks_jokes_and_includes_quicklinks(self):
        news = {
            "Fun": [
                {
                    "title": "Treffen sich zwei Blondinen",
                    "link": "http://hahaha.de/123",
                    "summary": 'Treffen sich zwei Blondinen.\n"Ich hatte gestern Führerscheinprüfung."',
                    "source": "Witz des Tages",
                }
            ],
            "Berlin": [
                {"title": f"Meldung {i}", "link": f"http://berlin.de/{i}", "summary": f"Text {i}"}
                for i in range(1, 6)
            ],
        }
        prompt, url_map = _build_briefing_prompt_and_urls(
            categorized_news=news,
            category_links={
                "Fun": "> 🔗 [Streamlit App](http://app/fun)",
                "Berlin": "> 🔗 [Streamlit App](http://app/berlin)",
            },
            lang_name="Deutsch",
            active_main_template=DEFAULT_MAIN_PROMPT_TEMPLATE,
            active_directives=DEFAULT_DIRECTIVES,
        )
        self.assertIn("## Berlin", prompt)
        self.assertIn("## Fun", prompt)
        self.assertIn("SPEZIALFALL: WITZ / HUMOR", prompt)
        self.assertIn("Treffen sich zwei Blondinen", prompt)
        self.assertIn("Führerscheinprüfung", prompt)
        self.assertEqual(url_map["http://hahaha.de/123"], "http://hahaha.de/123")

    def test_liked_articles_prioritized_in_briefing_prompt(self):
        # Meldung 9 ist geliked (feedback=1), Meldung 1 ist neutral, Meldung 2 ist disliked (feedback=-1)
        news = {
            "Berlin": [
                {"title": "Meldung 1 Neutral", "link": "http://berlin.de/1", "summary": "Text 1", "timestamp": 100.0, "feedback": 0},
                {"title": "Meldung 2 Disliked", "link": "http://berlin.de/2", "summary": "Text 2", "timestamp": 200.0, "feedback": -1},
                {"title": "Meldung 9 Liked", "link": "http://berlin.de/9", "summary": "Text 9", "timestamp": 50.0, "feedback": 1},
            ]
        }
        prompt, _ = _build_briefing_prompt_and_urls(
            categorized_news=news,
            category_links={},
            lang_name="Deutsch",
            active_main_template=DEFAULT_MAIN_PROMPT_TEMPLATE,
            active_directives=DEFAULT_DIRECTIVES,
        )
        pos_liked = prompt.find("Meldung 9 Liked")
        pos_neutral = prompt.find("Meldung 1 Neutral")
        pos_disliked = prompt.find("Meldung 2 Disliked")
        self.assertTrue(0 <= pos_liked < pos_neutral < pos_disliked)

    def test_email_rendering_with_quicklinks_and_multiline_joke(self):
        md = """## 🤖 Tech & AI

> 🔗 [Streamlit App](http://app)

- **[Titel 1](http://t1)**: Tatsächlicher Inhalt der Meldung ohne Schwurbeln.

## 😂 Fun

> 🔗 [Streamlit App](http://app/fun)

- **[Witz des Tages](http://joke)**:
Treffen sich zwei Blondinen.
"Ich hatte gestern Führerscheinprüfung."
"""
        html = markdown_to_html_email(md)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Streamlit App", html)
        self.assertIn("Tatsächlicher Inhalt der Meldung", html)
        self.assertIn("Treffen sich zwei Blondinen.", html)
        self.assertIn("Führerscheinprüfung.", html)

    def test_sources_yaml_and_canonical_prompt_match_strictly(self):
        """Prüft strikte 1:1 Übereinstimmung zwischen prompts.yaml/sources.yaml und kanonischem DEFAULT_MAIN_PROMPT_TEMPLATE / DIRECTIVES."""
        from src.aggregator import load_prompts, load_sources

        # 1. Teste direktes Laden über load_prompts
        prompts = load_prompts()
        prm_prompt = prompts.get("custom_main_prompt", "").strip()
        canonical_prompt = DEFAULT_MAIN_PROMPT_TEMPLATE.strip()
        self.assertEqual(
            prm_prompt,
            canonical_prompt,
            "custom_main_prompt in prompts.yaml weicht 1:1 von DEFAULT_MAIN_PROMPT_TEMPLATE ab!",
        )

        prm_directives = prompts.get("custom_prompt_directives", "").strip()
        canonical_directives = DEFAULT_DIRECTIVES.strip()
        self.assertTrue(
            canonical_directives in prm_directives or prm_directives.startswith(canonical_directives),
            "custom_prompt_directives in prompts.yaml muss die Basis-Richtlinien von DEFAULT_DIRECTIVES enthalten!",
        )

        # 2. Teste transparente Fassade über load_sources
        sources = load_sources(auto_reconcile=False)
        yaml_prompt = sources.get("settings", {}).get("custom_main_prompt", "").strip()
        self.assertEqual(
            yaml_prompt,
            canonical_prompt,
            "custom_main_prompt in load_sources weicht 1:1 von DEFAULT_MAIN_PROMPT_TEMPLATE ab!",
        )

        yaml_directives = sources.get("settings", {}).get("custom_prompt_directives", "").strip()
        self.assertTrue(
            canonical_directives in yaml_directives or yaml_directives.startswith(canonical_directives),
            "custom_prompt_directives in load_sources muss die Basis-Richtlinien von DEFAULT_DIRECTIVES enthalten!",
        )

    def test_reconcile_prompt_templates_initializes_missing_prompts(self):
        """Prüft, dass der Abgleich fehlende oder leere Prompts automatisch 1:1 mit den Standards initialisiert."""
        from src.aggregator import reconcile_prompt_templates

        # Fall 1: Leere Konfiguration -> initialisiert 1:1 mit den Standards
        empty_config = {}
        changed_empty = reconcile_prompt_templates(empty_config)
        self.assertTrue(changed_empty)
        self.assertEqual(empty_config["settings"]["custom_main_prompt"], DEFAULT_MAIN_PROMPT_TEMPLATE.strip())
        self.assertEqual(empty_config["settings"]["custom_prompt_directives"], DEFAULT_DIRECTIVES.strip())

        # Fall 2: Bereits initialisierte Konfiguration -> Keine erneute Änderung
        changed_again = reconcile_prompt_templates(empty_config)
        self.assertFalse(changed_again)

        # Fall 3: Vom Nutzer bewusst modifizierter Prompt -> Bleibt erhalten und wird nicht überschrieben
        custom_config = {"settings": {"custom_main_prompt": "Mein eigener benutzerdefinierter Redaktionsprompt"}}
        reconcile_prompt_templates(custom_config)
        self.assertEqual(custom_config["settings"]["custom_main_prompt"], "Mein eigener benutzerdefinierter Redaktionsprompt")


if __name__ == "__main__":
    unittest.main()
