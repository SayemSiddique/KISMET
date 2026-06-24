"""Interactive CLI entrypoint for KISMET."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.tree import Tree

from src.downloader import CategoryJob, HarvestReport, harvest, prune_empty_dirs
from src.llm import OLLAMA_TROUBLESHOOTING, BrainstormResult, OllamaConnectionError, brainstorm_categories
from src.utils import (
    DDG_TYPE_MAP,
    STYLE_MAP,
    build_search_query,
    build_stem,
    resolve_safe_path,
    sanitize_slug,
)

app = typer.Typer(add_completion=False, invoke_without_command=True)
console = Console()

_DEFAULT_IMAGES: int = 3
_MAX_IMAGES: int = 50

_NAMING_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("[item]_[index]",            "bmw_m3_01.jpg",            "[item]_[index]"),
    ("[category]_[item]_[index]", "cars_bmw_m3_01.jpg",       "[category]_[item]_[index]"),
    ("[custom_prefix]_[index]",   "photo_01.jpg  (you choose the prefix)", "[custom_prefix]_[index]"),
)

_STYLE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none",         "No preference  (default)"),
    ("product",      "Product photography  — clean, white background, studio lighting"),
    ("lifestyle",    "Lifestyle / in-use  — real-world setting, people, context"),
    ("editorial",    "Editorial / news  — documentary, journalistic"),
    ("illustration", "Illustration / vector art  — drawings, clipart, icons"),
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ItemEntry:
    display_name: str
    slug: str
    specification: str = ""  # optional per-item search enrichment


@dataclass
class CategoryEntry:
    display_name: str
    slug: str
    items: list[ItemEntry] = field(default_factory=list)


@dataclass
class SessionConfig:
    project_name: str
    project_slug: str
    collection_scope: str
    visual_style: str
    exclude_keywords: str
    categories: list[CategoryEntry]
    image_count: int
    save_dir: Path
    naming_pattern: str
    custom_prefix: str | None


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _prompt_project_name() -> tuple[str, str]:
    console.print()
    console.print("[bold]Step 1 — Collection Name[/bold]")
    name = Prompt.ask(
        "  What is this image collection called?\n"
        "  [dim](e.g. 'Car Website', 'Clothing Store', 'Restaurant Menu')[/dim]\n"
        "  [cyan]>[/cyan]",
        console=console,
    ).strip()
    if not name:
        console.print("[red]  Name cannot be empty.[/red]")
        raise typer.Exit(code=1)
    return name, sanitize_slug(name)


def _prompt_collection_scope() -> str:
    console.print()
    console.print("[bold]Step 2 — Collection Scope  [dim](fixes wrong-result problems)[/dim][/bold]")
    console.print(
        "  Describe your collection in a few words. This is prepended to every\n"
        "  search query so the engine knows the context.\n"
        "  [dim]e.g. 'baby girl clothing 6-12 months' / 'luxury cars 2024' / 'Indian street food'[/dim]\n"
        "  [dim]Leave blank to skip (less accurate results).[/dim]"
    )
    return Prompt.ask("  [cyan]>[/cyan]", default="", console=console).strip()


def _prompt_visual_style() -> str:
    console.print()
    console.print("[bold]Step 3 — Visual Style[/bold]")
    for i, (key, label) in enumerate(_STYLE_OPTIONS, 1):
        console.print(f"  [cyan]{i}[/cyan].  {label}")
    console.print()
    choice = Prompt.ask(
        "  Select style", choices=[str(i) for i in range(1, len(_STYLE_OPTIONS) + 1)],
        default="1", console=console,
    )
    return _STYLE_OPTIONS[int(choice) - 1][0]


def _prompt_exclude_keywords() -> str:
    console.print()
    console.print("[bold]Step 4 — Exclude Keywords  [dim](optional)[/dim][/bold]")
    console.print(
        "  Comma-separated words to exclude from every search.\n"
        "  [dim]e.g. 'cartoon, watermark, animated, clip art'  — Leave blank to skip.[/dim]"
    )
    return Prompt.ask("  [cyan]>[/cyan]", default="", console=console).strip()


def _prompt_destination(project_slug: str) -> Path:
    console.print()
    console.print("[bold]Step 5 — Save Location[/bold]")
    default = str(Path.home() / "Downloads" / f"kismet_{project_slug}")
    while True:
        raw = Prompt.ask(
            "  Where would you like to save the image folders?",
            default=default, console=console,
        )
        expanded = Path(raw).expanduser()
        if expanded == expanded.parent:
            console.print("  [red]Cannot write to the filesystem root.[/red]")
            continue
        try:
            return resolve_safe_path(expanded.parent, expanded.name)
        except PermissionError as exc:
            console.print(f"  [red]{exc}[/red]")


def _prompt_image_count() -> int:
    console.print()
    console.print("[bold]Step 6 — Images Per Item[/bold]")
    while True:
        n = IntPrompt.ask(
            f"  How many images per item?  [dim](1–{_MAX_IMAGES})[/dim]",
            default=_DEFAULT_IMAGES, console=console,
        )
        if 1 <= n <= _MAX_IMAGES:
            return n
        console.print(f"  [red]Enter a number between 1 and {_MAX_IMAGES}.[/red]")


def _prompt_naming_pattern() -> tuple[str, str | None]:
    console.print()
    console.print("[bold]Step 7 — File Naming Pattern[/bold]")
    for i, (pattern, example, _) in enumerate(_NAMING_OPTIONS, 1):
        console.print(f"  [cyan]{i}[/cyan].  {escape(pattern)}")
        console.print(f"      [dim]e.g. {escape(example)}[/dim]")
    console.print()

    choice = Prompt.ask(
        "  Select a naming option", choices=["1", "2", "3"], default="1", console=console,
    )
    pattern_template = _NAMING_OPTIONS[int(choice) - 1][2]
    custom_prefix: str | None = None

    if choice == "3":
        raw_prefix = Prompt.ask("  Enter your custom prefix", console=console).strip()
        custom_prefix = sanitize_slug(raw_prefix) or "image"
        pattern_template = f"{custom_prefix}_[index]"

    return pattern_template, custom_prefix


def _prompt_ai_assist(project_name: str, project_slug: str) -> list[CategoryEntry] | None:
    console.print()
    console.print("[bold]Step 1.5 — AI Category Suggest [dim](optional)[/dim][/bold]")
    try:
        result: BrainstormResult = brainstorm_categories(goal=project_name, category_count=5)
    except OllamaConnectionError:
        console.print(OLLAMA_TROUBLESHOOTING)
        return None

    console.print(f"  AI suggested [bold]{len(result.items)}[/bold] categories:\n")
    for i, item in enumerate(result.items, 1):
        console.print(f"  [cyan]{i}[/cyan].  [bold]{item.display_name}[/bold]  [dim]({item.folder_slug})[/dim]")
        console.print(f"      [dim]search: {item.search_query}[/dim]")
    console.print()

    answer = Prompt.ask(
        "  Use these? [dim](Y/n/edit)[/dim]",
        default="y",
        console=console,
    ).strip().lower()

    if answer in ("y", "yes", ""):
        return [
            CategoryEntry(display_name=it.display_name, slug=it.folder_slug)
            for it in result.items
        ]
    if answer in ("n", "no"):
        return None

    # edit — let user pick which to keep
    console.print("  Enter the numbers to keep, separated by commas [dim](e.g. 1,3,5)[/dim]:")
    raw = Prompt.ask("  [cyan]>[/cyan]", console=console).strip()
    kept: list[CategoryEntry] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(result.items):
                it = result.items[idx]
                kept.append(CategoryEntry(display_name=it.display_name, slug=it.folder_slug))
    return kept if kept else None


def _prompt_categories() -> list[CategoryEntry]:
    console.print()
    console.print(Rule("[bold]Step 8 — Define Your Categories & Items[/bold]", style="cyan"))
    console.print(
        "  Enter one category per line, press [bold]Enter on a blank line[/bold] when done.\n"
        "  [dim]e.g. BMW → Enter, Mercedes → Enter, (blank) → done[/dim]\n"
    )

    categories: list[CategoryEntry] = []
    index = 1
    while True:
        raw = Prompt.ask(f"  Category {index}", default="", console=console).strip()
        if not raw:
            if not categories:
                console.print("  [red]Enter at least one category.[/red]")
                continue
            break
        slug = sanitize_slug(raw)
        if not slug:
            console.print("  [red]Category name produced an empty slug — try again.[/red]")
            continue
        categories.append(CategoryEntry(display_name=raw, slug=slug))
        index += 1

    for cat in categories:
        console.print()
        console.print(
            f"  [bold cyan]{cat.display_name}[/bold cyan] — enter items "
            f"[dim](blank line when done)[/dim]"
        )
        item_index = 1
        while True:
            raw_item = Prompt.ask(f"    Item {item_index}", default="", console=console).strip()
            if not raw_item:
                if not cat.items:
                    console.print(f"  [red]Enter at least one item for '{cat.display_name}'.[/red]")
                    continue
                break
            item_slug = sanitize_slug(raw_item)
            if not item_slug:
                console.print("  [red]Item name produced an empty slug — try again.[/red]")
                continue
            spec = Prompt.ask(
                f"    Specification [dim](optional — e.g. 'pastel colors front view')[/dim]",
                default="", console=console,
            ).strip()
            cat.items.append(ItemEntry(display_name=raw_item, slug=item_slug, specification=spec))
            item_index += 1

    return categories


# ---------------------------------------------------------------------------
# Filename builder
# ---------------------------------------------------------------------------

def _build_filename(pattern: str, cat_slug: str, item_slug: str, index: int) -> str:
    return f"{build_stem(pattern, cat_slug, item_slug, index)}.jpg"


# ---------------------------------------------------------------------------
# Tree preview
# ---------------------------------------------------------------------------

def _build_tree(cfg: SessionConfig) -> Tree:
    root = Tree(f"[bold blue]{cfg.save_dir.parent}[/bold blue]", guide_style="dim blue")
    harvest_node = root.add(f"[bold]{cfg.save_dir.name}/[/bold]")

    for cat in cfg.categories:
        cat_node = harvest_node.add(f"[green]{cat.slug}/[/green]")
        for item in cat.items[:3]:
            item_node = cat_node.add(f"[yellow]{item.display_name}[/yellow]")
            for i in range(1, min(2, cfg.image_count) + 1):
                fname = _build_filename(cfg.naming_pattern, cat.slug, item.slug, i)
                item_node.add(f"[dim]{fname}[/dim]")
            if cfg.image_count > 2:
                item_node.add(f"[dim]… {cfg.image_count - 2} more[/dim]")
        if len(cat.items) > 3:
            cat_node.add(f"[dim]… {len(cat.items) - 3} more items[/dim]")

    return root


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _print_summary(cfg: SessionConfig) -> None:
    total_items = sum(len(c.items) for c in cfg.categories)
    style_label = next((l for k, l in _STYLE_OPTIONS if k == cfg.visual_style), cfg.visual_style)
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", width=22)
    table.add_column()

    table.add_row("Collection", cfg.project_name)
    if cfg.collection_scope:
        table.add_row("Scope", cfg.collection_scope)
    table.add_row("Visual style", style_label)
    if cfg.exclude_keywords:
        table.add_row("Exclude", cfg.exclude_keywords)
    table.add_row("Categories", str(len(cfg.categories)))
    table.add_row("Total items", str(total_items))
    table.add_row("Images per item", str(cfg.image_count))
    table.add_row("Total images", f"≈ {total_items * cfg.image_count}")
    table.add_row("Save path", str(cfg.save_dir))
    table.add_row("Naming", escape(cfg.naming_pattern.replace("[index]", "[01]")))

    console.print(table)


# ---------------------------------------------------------------------------
# Job builder
# ---------------------------------------------------------------------------

def _build_jobs(cfg: SessionConfig) -> list[CategoryJob]:
    style_suffix = STYLE_MAP.get(cfg.visual_style, "")
    ddg_filter = DDG_TYPE_MAP.get(cfg.visual_style, "")
    jobs: list[CategoryJob] = []

    for cat in cfg.categories:
        try:
            cat_dir = resolve_safe_path(cfg.save_dir, cat.slug)
        except PermissionError as exc:
            console.print(f"  [red]Skipping unsafe category '{cat.slug}': {exc}[/red]")
            continue
        for item in cat.items:
            query = build_search_query(
                item_display=item.display_name,
                collection_scope=cfg.collection_scope,
                item_spec=item.specification,
                style_suffix=style_suffix,
                exclude_keywords=cfg.exclude_keywords,
            )
            stems = [
                build_stem(cfg.naming_pattern, cat.slug, item.slug, i)
                for i in range(1, cfg.image_count + 1)
            ]
            jobs.append(CategoryJob(
                folder_slug=f"{cat.slug}/{item.slug}",
                search_query=query,
                dest_dir=cat_dir,
                filenames=stems,
                image_type_filter=ddg_filter,
            ))
    return jobs


# ---------------------------------------------------------------------------
# Query preview table (before download)
# ---------------------------------------------------------------------------

def _print_query_preview(cfg: SessionConfig) -> None:
    style_suffix = STYLE_MAP.get(cfg.visual_style, "")
    console.print()
    console.print(Rule("[bold]Effective Search Queries[/bold]", style="cyan"))
    console.print("  [dim]This is exactly what will be searched for each item:[/dim]\n")

    for cat in cfg.categories:
        console.print(f"  [bold green]{cat.display_name}[/bold green]")
        for item in cat.items:
            q = build_search_query(
                item_display=item.display_name,
                collection_scope=cfg.collection_scope,
                item_spec=item.specification,
                style_suffix=style_suffix,
                exclude_keywords=cfg.exclude_keywords,
            )
            console.print(f"    [yellow]{item.display_name}[/yellow] [dim]→[/dim] [cyan]\"{q}\"[/cyan]")
        console.print()


# ---------------------------------------------------------------------------
# Download runner
# ---------------------------------------------------------------------------

def _run_download(cfg: SessionConfig) -> HarvestReport:
    console.print()
    console.print(Rule("[bold]Downloading & Validating[/bold]", style="cyan"))

    jobs = _build_jobs(cfg)
    total_images = sum(len(j.filenames) for j in jobs)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    try:
        with progress:
            overall = progress.add_task("[cyan]Overall[/cyan]", total=total_images)

            def on_progress(event: str, folder_slug: str, _detail: str) -> None:
                if event == "saved":
                    progress.advance(overall)
                    progress.update(overall, description=f"[cyan]Saved[/cyan] [green]{folder_slug}[/green]")

            report = asyncio.run(harvest(jobs, on_progress=on_progress))
    except KeyboardInterrupt:
        removed = prune_empty_dirs(cfg.save_dir)
        console.print(
            f"\n[yellow]  Interrupted. Kept already-validated images; "
            f"removed {len(removed)} empty folder(s).[/yellow]"
        )
        raise typer.Exit(code=130)

    return report


def _print_download_report(report: HarvestReport) -> None:
    console.print()
    console.print(Rule("[bold]Harvest Report[/bold]", style="cyan"))

    table = Table(box=None, padding=(0, 2), header_style="bold dim")
    table.add_column("Category / Item", style="green")
    table.add_column("Saved", justify="right")
    table.add_column("Status")

    for cat in report.categories:
        if cat.error:
            status = f"[red]failed[/red] [dim]({cat.error})[/dim]"
        elif cat.saved_count < cat.requested:
            status = "[yellow]partial[/yellow]"
        else:
            status = "[green]complete[/green]"
        table.add_row(cat.folder_slug, f"{cat.saved_count}/{cat.requested}", status)

    console.print(table)
    console.print(
        f"\n  [bold]{report.total_saved}[/bold] of {report.total_requested} images "
        f"saved and validated.\n"
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Autonomous image harvesting agent — CLI or web UI."""
    if ctx.invoked_subcommand is None:
        try:
            _run_session()
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Session interrupted. Goodbye.[/yellow]")
            raise typer.Exit(code=0)


@app.command("web")
def web_command(
    port: int = typer.Option(8080, "--port", "-p", help="Port to run the web UI on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open browser."),
) -> None:
    """Launch the browser-based UI for KISMET."""
    import threading
    import webbrowser

    import uvicorn

    from src.web import build_app

    web_app = build_app()
    url = f"http://localhost:{port}"

    console.print(
        Panel.fit(
            f"[bold cyan]kismet web[/bold cyan]\n"
            f"[dim]Open your browser at[/dim] [bold]{url}[/bold]\n"
            f"[dim]Press Ctrl+C to stop the server.[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    if not no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="warning")


def _run_session() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]KISMET[/bold cyan]  [dim]v0.1.0 · Universal Image Harvesting Agent[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    project_name, project_slug = _prompt_project_name()
    ai_categories = _prompt_ai_assist(project_name, project_slug)
    collection_scope = _prompt_collection_scope()
    visual_style = _prompt_visual_style()
    exclude_keywords = _prompt_exclude_keywords()
    save_dir = _prompt_destination(project_slug)
    image_count = _prompt_image_count()
    naming_pattern, custom_prefix = _prompt_naming_pattern()
    categories = ai_categories if ai_categories is not None else _prompt_categories()

    cfg = SessionConfig(
        project_name=project_name,
        project_slug=project_slug,
        collection_scope=collection_scope,
        visual_style=visual_style,
        exclude_keywords=exclude_keywords,
        categories=categories,
        image_count=image_count,
        save_dir=save_dir,
        naming_pattern=naming_pattern,
        custom_prefix=custom_prefix,
    )

    console.print()
    console.print(Rule("[bold]Proposed Directory Structure[/bold]", style="cyan"))
    console.print(_build_tree(cfg))

    console.print(Rule("[bold]Session Summary[/bold]", style="cyan"))
    _print_summary(cfg)

    _print_query_preview(cfg)

    if not Confirm.ask("  Proceed with image search and download?", console=console):
        console.print("\n[yellow]  Aborted. No files were written.[/yellow]")
        raise typer.Exit(code=0)

    report = _run_download(cfg)
    _print_download_report(report)
