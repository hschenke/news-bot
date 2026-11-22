# Projekt-Richtlinien für News Bot

## Automatischer Start-Ablauf
- **Automatisches `git pull`**: Führe zu Beginn jeder neuen Konversation bzw. als allererste Aktion bei der Bearbeitung der ersten Benutzeranfrage in diesem Projekt immer automatisch `git pull` aus, um das lokale Repository auf den neuesten Stand zu bringen.
- Gib dem Benutzer kurz Rückmeldung über den Status/Ergebnis der Aktualisierung.

## Automatischer Abschluss-Ablauf & Versions-Tags (Semantic Versioning)
- **Automatisches Versionieren, Committen, Taggen & Pushen**: Nach jeder abgeschlossenen Umsetzung von Änderungen oder neuen Features immer automatisch und **ohne vorherige Rückfrage**:
  1. Nächste Versionsnummer nach Semantic Versioning anhand des letzten Tags (`git describe --tags --abbrev=0`) bestimmen:
     - **Minor (`v0.X.0`)**: Bei neuen Features, neuen Seiten/Modulen oder signifikanten UI-/Architektur-Erweiterungen.
     - **Patch (`v0.X.Y`)**: Bei Bugfixes, Dependency-/Workflow-Updates oder kleineren Detailanpassungen.
  2. `__version__` in `src/__version__.py` auf die neue Version aktualisieren.
  3. Alle Änderungen committen, den annotierten Git-Tag erstellen (`git tag -a <tag> -m "Release <tag>: ..."`) und sofort mit `git push origin main --follow-tags` zu GitHub hochladen, damit Änderungen und Version sofort online auf GitHub und Streamlit Cloud aktiv sind.
- Gib dem Benutzer eine Bestätigung mit Commit-Hash, Nachricht und gesetztem Versions-Tag.

## Python Clean Code Standard
- **Verbindliche Beachtung des `python-clean-code` Skills**: Bei jeglicher Programmier- und Refactoring-Arbeit in diesem Projekt MUSS der projektinterne Skill [.agents/skills/python-clean-code/SKILL.md](file:///c:/Users/holge/.gemini/antigravity/scratch/news-aggregator-bot/.agents/skills/python-clean-code/SKILL.md) strikt beachtet und angewendet werden:
  - **Moderne Typisierung & Datenmodelle**: Verwendung moderner Type Hints (`str | None`, `list[...]`, `dict[...]`) und strukturierter Daten (`@dataclass(frozen=True)` / Pydantic) statt loser Dictionaries mit Magic Keys.
  - **Netzwerk-Resilienz & Fehlerbehandlung**: Explizite Timeouts bei jedem HTTP-Call, Retry-Strategien mit Exponential Backoff, isoliertes Abfangen und Protokollieren von Einzelfeed-Fehlern, keine Silent Fails (`except: pass`), spezifische Exceptions.
  - **Logging**: Durchgängige Verwendung des Standard-Moduls `logging` anstelle von `print()`.
  - **Separation of Concerns**: Strikte architektonische Trennung (Ingestion/Scraper -> Processing/Transformation -> Storage/Persistence -> Presentation/UI). Keine Business-Logik oder Scraping-Code in Streamlit-Views.
  - **Refactoring-Workflow**: 4-Stufen-Ablauf einhalten (Analyse & Smell-Audit -> Refactoring-Plan -> Durchführung -> Verifikation/Tests).

## Security & Secure Coding Standard (Basis-Requirement)
- **Verbindliche Beachtung des `security-standard` Skills**: Bei jeglicher Programmier-, Integrations- und Refactoring-Arbeit in diesem Projekt MUSS der Skill [.agents/skills/security-standard/SKILL.md](file:///c:/Users/holge/.gemini/antigravity/scratch/news-aggregator-bot/.agents/skills/security-standard/SKILL.md) als permanentes Basis-Requirement beachtet und angewendet werden:
  - **Code Security**: Keine Secrets/Tokens/API-Keys im Quellcode; strikte Konfiguration via `.env` und Streamlit Secrets; saubere Input-Validierung; sichere Pfad-Behandlung (kein Path Traversal); sichere Subprozess-Ausführung (`shell=False`).
  - **OWASP Compliance**: Schutz vor Injection (SQL/Command/XSS), sichere Deserialisierung (kein `pickle.loads`), sichere Fehlerbehandlung (Fail-Secure).
  - **SSRF-Schutz für Scraper & Aggregatoren**: Validierung aller RSS/News-URLs auf zulässige Schemata (`http`/`https`); Ausschluss lokaler und privater IP-Bereiche (`127.0.0.1`, RFC 1918, Cloud-Metadaten `169.254.169.254`).
  - **Logging & Privacy**: Keine Tokens, Passwörter oder personenbezogene Daten (PII) in Anwendungs-Logs ausgeben.
  - **Sicherheits-Checkliste vor Commit**: Vor jedem Git-Commit die 6-Punkte-Checkliste aus dem Skill prüfen.




