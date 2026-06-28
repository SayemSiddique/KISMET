"""Locale-aware message catalog for KISMET."""

import locale
import os

_CATALOG: dict[str, dict[str, str]] = {
    "en": {
        "step1_header": "[bold]Step 1 — Collection Name[/bold]",
        "step1_prompt": (
            "  What is this image collection called?\n"
            "  [dim](e.g. 'Car Website', 'Clothing Store', 'Restaurant Menu')[/dim]\n"
            "  [cyan]>[/cyan]"
        ),
        "step1_error": "[red]  Name cannot be empty.[/red]",
        "step2_header": (
            "[bold]Step 2 — Collection Scope  "
            "[dim](fixes wrong-result problems)[/dim][/bold]"
        ),
        "step3_header": "[bold]Step 3 — Visual Style[/bold]",
        "step4_header": "[bold]Step 4 — Exclude Keywords  [dim](optional)[/dim][/bold]",
        "step5_header": "[bold]Step 5 — Save Location[/bold]",
        "step6_header": "[bold]Step 6 — Images Per Item[/bold]",
        "step7_header": "[bold]Step 7 — File Naming Pattern[/bold]",
        "step8_header": "Step 8 — Define Your Categories & Items",
        "ai_assist_header": (
            "[bold]Step 1.5 — AI Category Suggest [dim](optional)[/dim][/bold]"
        ),
        "proceed_confirm": "  Proceed with image search and download?",
        "aborted": "\n[yellow]  Aborted. No files were written.[/yellow]",
        "interrupted": "\n\n[yellow]Session interrupted. Goodbye.[/yellow]",
        "downloading_header": "[bold]Downloading & Validating[/bold]",
        "dry_run_header": "[bold]Dry Run — Preview Only (no files written)[/bold]",
        "harvest_report_header": "[bold]Harvest Report[/bold]",
        "export_header": "[bold]Export[/bold]",
        "dir_structure_header": "[bold]Proposed Directory Structure[/bold]",
        "session_summary_header": "[bold]Session Summary[/bold]",
        "search_queries_header": "[bold]Effective Search Queries[/bold]",
        "search_queries_hint": "  [dim]This is exactly what will be searched for each item:[/dim]",
    },
    "es": {
        "step1_header": "[bold](es) Paso 1 — Nombre de la colección[/bold]",
        "step1_prompt": (
            "  ¿Cómo se llama esta colección de imágenes?\n"
            "  [dim](ej. 'Sitio de autos', 'Tienda de ropa', 'Menú del restaurante')[/dim]\n"
            "  [cyan]>[/cyan]"
        ),
        "step1_error": "[red]  El nombre no puede estar vacío.[/red]",
        "step2_header": (
            "[bold](es) Paso 2 — Alcance de la colección  "
            "[dim](evita resultados incorrectos)[/dim][/bold]"
        ),
        "step3_header": "[bold](es) Paso 3 — Estilo visual[/bold]",
        "step4_header": (
            "[bold](es) Paso 4 — Palabras clave excluidas  [dim](opcional)[/dim][/bold]"
        ),
        "step5_header": "[bold](es) Paso 5 — Ubicación de guardado[/bold]",
        "step6_header": "[bold](es) Paso 6 — Imágenes por elemento[/bold]",
        "step7_header": "[bold](es) Paso 7 — Patrón de nombre de archivo[/bold]",
        "step8_header": "(es) Paso 8 — Definir categorías y elementos",
        "ai_assist_header": (
            "[bold](es) Paso 1.5 — Sugerencia de categorías por IA "
            "[dim](opcional)[/dim][/bold]"
        ),
        "proceed_confirm": (
            "  (es) ¿Continuar con la búsqueda y descarga de imágenes?"
        ),
        "aborted": "\n[yellow]  (es) Cancelado. No se escribieron archivos.[/yellow]",
        "interrupted": "\n\n[yellow](es) Sesión interrumpida. Hasta luego.[/yellow]",
        "downloading_header": "[bold](es) Descargando y validando[/bold]",
        "dry_run_header": (
            "[bold](es) Simulacro — Solo vista previa (sin archivos escritos)[/bold]"
        ),
        "harvest_report_header": "[bold](es) Informe de recolección[/bold]",
        "export_header": "[bold](es) Exportar[/bold]",
        "dir_structure_header": "[bold](es) Estructura de directorios propuesta[/bold]",
        "session_summary_header": "[bold](es) Resumen de sesión[/bold]",
        "search_queries_header": "[bold](es) Consultas de búsqueda efectivas[/bold]",
        "search_queries_hint": (
            "  [dim](es) Esto es exactamente lo que se buscará para cada elemento:[/dim]"
        ),
    },
    "fr": {
        "step1_header": "[bold](fr) Étape 1 — Nom de la collection[/bold]",
        "step1_prompt": (
            "  Comment s'appelle cette collection d'images?\n"
            "  [dim](ex. 'Site voitures', 'Boutique vêtements', 'Menu restaurant')[/dim]\n"
            "  [cyan]>[/cyan]"
        ),
        "step1_error": "[red]  Le nom ne peut pas être vide.[/red]",
        "step2_header": (
            "[bold](fr) Étape 2 — Portée de la collection  "
            "[dim](évite les mauvais résultats)[/dim][/bold]"
        ),
        "step3_header": "[bold](fr) Étape 3 — Style visuel[/bold]",
        "step4_header": (
            "[bold](fr) Étape 4 — Mots-clés exclus  [dim](facultatif)[/dim][/bold]"
        ),
        "step5_header": "[bold](fr) Étape 5 — Emplacement de sauvegarde[/bold]",
        "step6_header": "[bold](fr) Étape 6 — Images par élément[/bold]",
        "step7_header": "[bold](fr) Étape 7 — Modèle de nom de fichier[/bold]",
        "step8_header": "(fr) Étape 8 — Définir les catégories et les éléments",
        "ai_assist_header": (
            "[bold](fr) Étape 1.5 — Suggestion de catégories par IA "
            "[dim](facultatif)[/dim][/bold]"
        ),
        "proceed_confirm": (
            "  (fr) Procéder à la recherche et au téléchargement d'images?"
        ),
        "aborted": "\n[yellow]  (fr) Annulé. Aucun fichier écrit.[/yellow]",
        "interrupted": "\n\n[yellow](fr) Session interrompue. Au revoir.[/yellow]",
        "downloading_header": "[bold](fr) Téléchargement et validation[/bold]",
        "dry_run_header": (
            "[bold](fr) Simulation — Aperçu uniquement (aucun fichier écrit)[/bold]"
        ),
        "harvest_report_header": "[bold](fr) Rapport de récolte[/bold]",
        "export_header": "[bold](fr) Exporter[/bold]",
        "dir_structure_header": "[bold](fr) Structure de répertoire proposée[/bold]",
        "session_summary_header": "[bold](fr) Résumé de session[/bold]",
        "search_queries_header": "[bold](fr) Requêtes de recherche effectives[/bold]",
        "search_queries_hint": (
            "  [dim](fr) C'est exactement ce qui sera recherché pour chaque élément:[/dim]"
        ),
    },
}

_LOCALE_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish (Español)",
    "fr": "French (Français)",
}

_active_locale: str = "en"


def available_locales() -> list[tuple[str, str]]:
    """Return (code, display_name) pairs for all supported locales, sorted by code."""
    return [(code, _LOCALE_DISPLAY_NAMES.get(code, code)) for code in sorted(_CATALOG)]


def resolve_locale(lang: str) -> str:
    """Normalize a lang string to a supported locale code, falling back to 'en'."""
    if not lang:
        return "en"
    if lang in _CATALOG:
        return lang
    prefix = lang.replace("-", "_").split("_")[0].lower()
    if prefix in _CATALOG:
        return prefix
    return "en"


def detect_system_locale() -> str:
    """Detect the system locale and map it to a supported locale code."""
    try:
        lang, _ = locale.getlocale()
        if lang:
            return resolve_locale(lang)
    except Exception:  # noqa: BLE001
        pass
    return "en"


def init_locale(lang: str = "") -> str:
    """Initialize the active locale from explicit lang, KISMET_LANG env, or system locale.

    Returns the resolved locale code.
    """
    global _active_locale  # noqa: PLW0603
    if lang:
        _active_locale = resolve_locale(lang)
    else:
        env_lang = os.environ.get("KISMET_LANG", "")
        _active_locale = resolve_locale(env_lang) if env_lang else detect_system_locale()
    return _active_locale


def get_active_locale() -> str:
    """Return the currently active locale code."""
    return _active_locale


def get_text(key: str) -> str:
    """Return the localized string for *key* in the active locale.

    Falls back to the 'en' catalog entry, then the key itself if missing entirely.
    """
    catalog = _CATALOG.get(_active_locale, _CATALOG["en"])
    return catalog.get(key, _CATALOG["en"].get(key, key))


def catalog_keys() -> frozenset[str]:
    """Return the canonical set of message keys (from the 'en' catalog)."""
    return frozenset(_CATALOG["en"])
