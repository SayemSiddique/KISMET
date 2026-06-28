"""Interactive CLI entrypoint for KISMET."""

import asyncio
import datetime
import json as _json
import logging
import os
import sys
import traceback
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

from src.config import (
    ExportDefaults,
    KismetConfig,
    PostprocessDefaults,
    ProfileConfig,
    load_config,
    load_discovery_config,
)
from src.downloader import CategoryJob, HarvestReport, build_provider, harvest, prune_empty_dirs
from src.export import (
    ExportConfig,
    export_contact_sheet,
    export_ml_dataset,
    export_thumbnails,
    export_web,
    export_zip,
)
from src.i18n import available_locales, get_text, init_locale
from src.llm import (
    OLLAMA_TROUBLESHOOTING,
    BrainstormResult,
    OllamaConnectionError,
    brainstorm_categories,
)
from src.plugins import PluginRegistry, load_dotted_path, load_entry_points
from src.postprocess import PostprocessConfig, PostprocessPipeline
from src.scoring import Scorer, build_scorer
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


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("kismet")
    except Exception:  # noqa: BLE001
        return "0.1.0"


def _version_callback(value: bool) -> None:
    if value:
        print(f"kismet {_get_version()}")
        raise typer.Exit()

_kismet_logger = logging.getLogger("kismet")


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "event": record.getMessage(),
            "details": getattr(record, "details", {}),
        }
        return _json.dumps(payload)


def _setup_log_file(path: str) -> None:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_JSONFormatter())
    _kismet_logger.setLevel(logging.DEBUG)
    _kismet_logger.addHandler(handler)


def _log(level: str, event: str, **details: object) -> None:
    record = _kismet_logger.makeRecord(
        "kismet",
        getattr(logging, level.upper(), logging.INFO),
        "",
        0,
        event,
        (),
        None,
    )
    record.details = details  # type: ignore[attr-defined]
    _kismet_logger.handle(record)


_DEFAULT_IMAGES: int = 3
_MAX_IMAGES: int = 50

_NAMING_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("[item]_[index]", "bmw_m3_01.jpg", "[item]_[index]"),
    ("[category]_[item]_[index]", "cars_bmw_m3_01.jpg", "[category]_[item]_[index]"),
    ("[custom_prefix]_[index]", "photo_01.jpg  (you choose the prefix)", "[custom_prefix]_[index]"),
)

_STYLE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "No preference  (default)"),
    ("product", "Product photography  — clean, white background, studio lighting"),
    ("lifestyle", "Lifestyle / in-use  — real-world setting, people, context"),
    ("editorial", "Editorial / news  — documentary, journalistic"),
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
    console.print(get_text("step1_header"))
    name = Prompt.ask(get_text("step1_prompt"), console=console).strip()
    if not name:
        console.print(get_text("step1_error"))
        raise typer.Exit(code=1)
    return name, sanitize_slug(name)


def _prompt_collection_scope() -> str:
    console.print()
    console.print(get_text("step2_header"))
    console.print(
        "  Describe your collection in a few words. This is prepended to every\n"
        "  search query so the engine knows the context.\n"
        "  [dim]e.g. 'baby girl clothing 6-12 months' / 'luxury cars 2024'"
        " / 'Indian street food'[/dim]\n"
        "  [dim]Leave blank to skip (less accurate results).[/dim]"
    )
    return Prompt.ask("  [cyan]>[/cyan]", default="", console=console).strip()


def _prompt_visual_style() -> str:
    console.print()
    console.print(get_text("step3_header"))
    for i, (_key, label) in enumerate(_STYLE_OPTIONS, 1):
        console.print(f"  [cyan]{i}[/cyan].  {label}")
    console.print()
    choice = Prompt.ask(
        "  Select style",
        choices=[str(i) for i in range(1, len(_STYLE_OPTIONS) + 1)],
        default="1",
        console=console,
    )
    return _STYLE_OPTIONS[int(choice) - 1][0]


def _prompt_exclude_keywords() -> str:
    console.print()
    console.print(get_text("step4_header"))
    console.print(
        "  Comma-separated words to exclude from every search.\n"
        "  [dim]e.g. 'cartoon, watermark, animated, clip art'  — Leave blank to skip.[/dim]"
    )
    return Prompt.ask("  [cyan]>[/cyan]", default="", console=console).strip()


def _prompt_destination(project_slug: str) -> Path:
    console.print()
    console.print(get_text("step5_header"))
    default = str(Path.home() / "Downloads" / f"kismet_{project_slug}")
    while True:
        raw = Prompt.ask(
            "  Where would you like to save the image folders?",
            default=default,
            console=console,
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
    console.print(get_text("step6_header"))
    while True:
        n = IntPrompt.ask(
            f"  How many images per item?  [dim](1–{_MAX_IMAGES})[/dim]",
            default=_DEFAULT_IMAGES,
            console=console,
        )
        if 1 <= n <= _MAX_IMAGES:
            return n
        console.print(f"  [red]Enter a number between 1 and {_MAX_IMAGES}.[/red]")


def _prompt_naming_pattern() -> tuple[str, str | None]:
    console.print()
    console.print(get_text("step7_header"))
    for i, (pattern, example, _) in enumerate(_NAMING_OPTIONS, 1):
        console.print(f"  [cyan]{i}[/cyan].  {escape(pattern)}")
        console.print(f"      [dim]e.g. {escape(example)}[/dim]")
    console.print()

    choice = Prompt.ask(
        "  Select a naming option",
        choices=["1", "2", "3"],
        default="1",
        console=console,
    )
    pattern_template = _NAMING_OPTIONS[int(choice) - 1][2]
    custom_prefix: str | None = None

    if choice == "3":
        raw_prefix = Prompt.ask("  Enter your custom prefix", console=console).strip()
        custom_prefix = sanitize_slug(raw_prefix) or "image"
        pattern_template = f"{custom_prefix}_[index]"

    return pattern_template, custom_prefix


def _prompt_ai_assist(
    project_name: str, project_slug: str
) -> tuple[list[CategoryEntry] | None, BrainstormResult | None]:
    console.print()
    console.print(get_text("ai_assist_header"))
    try:
        result: BrainstormResult = brainstorm_categories(goal=project_name, category_count=5)
    except OllamaConnectionError:
        console.print(OLLAMA_TROUBLESHOOTING)
        return None, None

    console.print(f"  AI suggested [bold]{len(result.items)}[/bold] categories:\n")
    for i, item in enumerate(result.items, 1):
        count_hint = result.per_category_counts.get(item.folder_slug, "")
        count_str = f"  [dim]~{count_hint} images[/dim]" if count_hint else ""
        console.print(
            f"  [cyan]{i}[/cyan].  [bold]{item.display_name}[/bold]"
            f"  [dim]({item.folder_slug})[/dim]{count_str}"
        )
        console.print(f"      [dim]search: {item.search_query}[/dim]")
    if result.image_type_filter:
        console.print(f"\n  [dim]Suggested image type filter: {result.image_type_filter}[/dim]")
    console.print()

    answer = (
        Prompt.ask(
            "  Use these? [dim](Y/n/edit)[/dim]",
            default="y",
            console=console,
        )
        .strip()
        .lower()
    )

    if answer in ("y", "yes", ""):
        return [
            CategoryEntry(display_name=it.display_name, slug=it.folder_slug) for it in result.items
        ], result
    if answer in ("n", "no"):
        return None, None

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
    return (kept if kept else None), result


def _prompt_categories() -> list[CategoryEntry]:
    console.print()
    console.print(Rule(get_text("step8_header"), style="cyan"))
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
                "    Specification [dim](optional — e.g. 'pastel colors front view')[/dim]",
                default="",
                console=console,
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
    style_label = next(
        (lbl for k, lbl in _STYLE_OPTIONS if k == cfg.visual_style), cfg.visual_style
    )
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


def _build_jobs(
    cfg: SessionConfig, *, min_score: float = 0.0, pipeline: PostprocessPipeline | None = None
) -> list[CategoryJob]:
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
            jobs.append(
                CategoryJob(
                    folder_slug=f"{cat.slug}/{item.slug}",
                    search_query=query,
                    dest_dir=cat_dir,
                    filenames=stems,
                    image_type_filter=ddg_filter,
                    min_score=min_score,
                    postprocess=pipeline,
                )
            )
    return jobs


# ---------------------------------------------------------------------------
# Query preview table (before download)
# ---------------------------------------------------------------------------


def _print_query_preview(cfg: SessionConfig) -> None:
    style_suffix = STYLE_MAP.get(cfg.visual_style, "")
    console.print()
    console.print(Rule(get_text("search_queries_header"), style="cyan"))
    console.print(get_text("search_queries_hint") + "\n")

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
            console.print(
                f'    [yellow]{item.display_name}[/yellow] [dim]→[/dim] [cyan]"{q}"[/cyan]'
            )
        console.print()


# ---------------------------------------------------------------------------
# Download runner
# ---------------------------------------------------------------------------


def _run_download(
    cfg: SessionConfig,
    *,
    resume: bool = True,
    use_cache: bool = True,
    require_license: bool = False,
    dedup_threshold: int = 4,
    scorer: Scorer | None = None,
    min_score: float = 0.0,
    pipeline: PostprocessPipeline | None = None,
    dry_run: bool = False,
    plugin_registry: PluginRegistry | None = None,
) -> HarvestReport:
    console.print()
    if dry_run:
        console.print(Rule(get_text("dry_run_header"), style="yellow"))
    else:
        console.print(Rule(get_text("downloading_header"), style="cyan"))

    jobs = _build_jobs(cfg, min_score=min_score, pipeline=pipeline)
    total_images = sum(len(j.filenames) for j in jobs)

    _log(
        "info",
        "harvest_start",
        project=cfg.project_name,
        total_requested=total_images,
        dry_run=dry_run,
    )

    if dry_run:

        def on_progress(event: str, folder_slug: str, detail: str) -> None:
            if event == "dry_run":
                try:
                    info = _json.loads(detail)
                    console.print(
                        f"  [dim]would download[/dim] "
                        f"[green]{folder_slug}/{info['filename']}[/green] "
                        f"from [cyan]{info['provider'] or 'provider'}[/cyan] "
                        f"[dim]{info['url'][:80]}[/dim]"
                    )
                except (ValueError, KeyError):
                    console.print(f"  [dim]would download[/dim] {folder_slug}")
                _log(
                    "info",
                    "dry_run_candidate",
                    folder_slug=folder_slug,
                    **(_json.loads(detail) if detail else {}),
                )
    else:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        overall = progress.add_task("[cyan]Overall[/cyan]", total=total_images)

        def on_progress(event: str, folder_slug: str, detail: str) -> None:
            if event == "saved":
                progress.advance(overall)
                progress.update(
                    overall, description=f"[cyan]Saved[/cyan] [green]{folder_slug}[/green]"
                )
                _log("info", "image_saved", folder_slug=folder_slug, path=detail)
            elif event == "skipped":
                progress.advance(overall)
                progress.update(
                    overall, description=f"[cyan]Resumed[/cyan] [dim]{folder_slug}[/dim]"
                )
                _log("debug", "image_skipped", folder_slug=folder_slug)
            elif event == "duplicate":
                _log("debug", "image_duplicate", folder_slug=folder_slug, url=detail)
            elif event == "filtered":
                _log("debug", "image_filtered", folder_slug=folder_slug, url=detail)

    cache_dir = _DISCOVERY_CACHE_DIR if use_cache else None
    provider = build_provider(load_discovery_config(), cache_dir=cache_dir)

    try:
        if dry_run:
            report = asyncio.run(
                harvest(
                    jobs,
                    provider=provider,
                    on_progress=on_progress,
                    resume=resume,
                    state_path=cfg.save_dir / _STATE_FILENAME if not dry_run else None,
                    require_license=require_license,
                    dedup_threshold=dedup_threshold,
                    scorer=None,
                    dry_run=True,
                    plugin_registry=plugin_registry,
                )
            )
        else:
            with progress:
                report = asyncio.run(
                    harvest(
                        jobs,
                        provider=provider,
                        on_progress=on_progress,
                        resume=resume,
                        state_path=cfg.save_dir / _STATE_FILENAME,
                        require_license=require_license,
                        dedup_threshold=dedup_threshold,
                        scorer=scorer if min_score > 0.0 else None,
                        plugin_registry=plugin_registry,
                    )
                )
    except KeyboardInterrupt:
        if not dry_run:
            removed = prune_empty_dirs(cfg.save_dir)
            console.print(
                f"\n[yellow]  Interrupted. Kept already-validated images; "
                f"removed {len(removed)} empty folder(s).[/yellow]"
            )
        raise typer.Exit(code=130) from None

    _log(
        "info",
        "harvest_complete",
        total_saved=report.total_saved,
        total_skipped=report.total_skipped,
        provider_hit_rate=report.provider_hit_rate,
        license_breakdown=report.license_breakdown,
    )
    return report


def _print_download_report(report: HarvestReport) -> None:
    console.print()
    console.print(Rule(get_text("harvest_report_header"), style="cyan"))

    table = Table(box=None, padding=(0, 2), header_style="bold dim")
    table.add_column("Category / Item", style="green")
    table.add_column("Saved", justify="right")
    table.add_column("Status")

    for cat in report.categories:
        if cat.error:
            status = f"[red]failed[/red] [dim]({cat.error})[/dim]"
        elif cat.present_count < cat.requested:
            status = "[yellow]partial[/yellow]"
        else:
            status = "[green]complete[/green]"
        if cat.skipped_count:
            status += f" [dim](+{cat.skipped_count} resumed)[/dim]"
        table.add_row(cat.folder_slug, f"{cat.present_count}/{cat.requested}", status)

    console.print(table)
    resumed_note = (
        f"  [dim]{report.total_skipped} already on disk (resumed).[/dim]\n"
        if report.total_skipped
        else ""
    )
    dedup_note = (
        f"  [dim]{report.total_deduplicated} near-duplicate(s) skipped.[/dim]\n"
        if report.total_deduplicated
        else ""
    )
    filtered_note = (
        f"  [dim]{report.total_filtered} image(s) dropped by relevance scorer.[/dim]\n"
        if report.total_filtered
        else ""
    )
    console.print(
        f"\n  [bold]{report.total_saved}[/bold] new image(s) saved; "
        f"{report.total_saved + report.total_skipped} of {report.total_requested} present.\n"
        f"{resumed_note}"
        f"{dedup_note}"
        f"{filtered_note}"
    )

    hit_rate = report.provider_hit_rate
    if hit_rate:
        rate_str = "  ".join(f"[cyan]{p}[/cyan] {n}" for p, n in sorted(hit_rate.items()))
        console.print(f"  [dim]Provider hits:[/dim]  {rate_str}")

    lic = report.license_breakdown
    if lic:
        lic_str = "  ".join(f"[dim]{lk}[/dim] {n}" for lk, n in sorted(lic.items()))
        console.print(f"  [dim]Licenses:[/dim]  {lic_str}")


# ---------------------------------------------------------------------------
# Post-process helpers
# ---------------------------------------------------------------------------


def _make_postprocess_pipeline(
    defs: "PostprocessDefaults | None",
    *,
    resize_max_px: int,
    crop_aspect: str,
    downscale_kb: int,
    auto_orient: bool,
    remove_bg: bool,
) -> PostprocessPipeline | None:
    """Build a PostprocessPipeline from CLI flags (override) + config defaults (fallback).

    Returns None when all transforms are effectively disabled.
    """
    d = defs or PostprocessDefaults()
    cfg = PostprocessConfig(
        resize_max_px=resize_max_px if resize_max_px != 0 else d.resize_max_px,
        crop_aspect=crop_aspect if crop_aspect else d.crop_aspect,
        downscale_kb=downscale_kb if downscale_kb != 0 else d.downscale_kb,
        auto_orient=auto_orient,
        remove_bg=remove_bg or d.remove_bg,
    )
    pipeline = PostprocessPipeline(cfg)
    return None if pipeline.is_noop() else pipeline


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _resolve_export_dir(save_dir: Path, override: str) -> Path:
    if override:
        return Path(override).expanduser()
    return save_dir / "export"


def _make_export_cfg(defs: "ExportDefaults | None", fmt: str) -> ExportConfig:
    if defs is None:
        return ExportConfig(output_format=fmt)
    raw_ts = defs.thumbnail_size[:2] if len(defs.thumbnail_size) >= 2 else [256, 256]
    raw_sp = defs.split[:3] if len(defs.split) >= 3 else [0.7, 0.15, 0.15]
    ts: tuple[int, int] = (int(raw_ts[0]), int(raw_ts[1]))
    sp: tuple[float, float, float] = (float(raw_sp[0]), float(raw_sp[1]), float(raw_sp[2]))
    return ExportConfig(
        webp_quality=defs.webp_quality,
        max_width=defs.max_width,
        thumbnail_size=ts,
        contact_sheet_cols=defs.contact_sheet_cols,
        split=sp,
        output_format=fmt or defs.output_format,
    )


# ---------------------------------------------------------------------------
# Export runner
# ---------------------------------------------------------------------------


def _run_export(
    report: HarvestReport,
    export_dir: Path,
    export_cfg: ExportConfig,
    *,
    do_thumbnails: bool = True,
    do_contact_sheet: bool = False,
    do_zip: bool = False,
    do_ml_dataset: bool = False,
) -> None:
    console.print()
    console.print(Rule(get_text("export_header"), style="cyan"))
    web_paths = export_web(report, export_dir, export_cfg)
    console.print(
        f"  [green]{len(web_paths)}[/green] web-optimised image(s) → [dim]{export_dir}[/dim]"
    )

    if do_thumbnails:
        thumb_paths = export_thumbnails(report, export_dir, export_cfg)
        console.print(
            f"  [green]{len(thumb_paths)}[/green] thumbnail(s)"
            f" → [dim]{export_dir / 'thumbnails'}[/dim]"
        )

    if do_contact_sheet:
        sheet_path = export_contact_sheet(report, export_dir, export_cfg)
        console.print(f"  Contact sheet → [dim]{sheet_path}[/dim]")

    if do_ml_dataset:
        manifest_path = export_ml_dataset(report, export_dir, export_cfg)
        console.print(f"  ML dataset manifest → [dim]{manifest_path}[/dim]")

    if do_zip:
        zip_path = export_dir.parent / (export_dir.name + ".zip")
        export_zip(export_dir, zip_path)
        console.print(f"  Archive → [dim]{zip_path}[/dim]")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


@app.command("completion")
def completion_command(
    shell: str = typer.Argument(..., help="Shell type: bash, zsh, or fish"),
) -> None:
    """Print shell tab-completion script for KISMET."""
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    _classes = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}
    cls = _classes.get(shell.lower())
    if cls is None:
        console.print(f"[red]Error: Unknown shell '{shell}'. Supported: bash, zsh, fish[/red]")
        raise typer.Exit(code=1)
    cli = typer.main.get_group(app)
    comp = cls(cli, {}, "kismet", "_KISMET_COMPLETE")  # type: ignore[arg-type]
    print(comp.source())


@app.command("config")
def config_command(
    config_path: str = typer.Option(
        "",
        "--config",
        help="Path to a config.toml file (default: ~/.kismet/config.toml).",
    ),
) -> None:
    """Show the resolved KISMET configuration as pretty JSON."""
    cfg_file = Path(config_path).expanduser() if config_path else None
    kismet_cfg = load_config(cfg_file)
    print(_json.dumps(kismet_cfg.model_dump(), indent=2, default=str))


@app.command("langs")
def langs_command() -> None:
    """List available locales for KISMET's CLI."""
    table = Table(show_header=True, box=None, padding=(0, 2), header_style="bold dim")
    table.add_column("Code")
    table.add_column("Language")
    for code, name in available_locales():
        table.add_row(f"[cyan]{code}[/cyan]", name)
    console.print(table)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(  # noqa: FBT001
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Skip images already on disk so re-runs continue where they left off.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Disable the on-disk discovery cache (always re-query providers).",
    ),
    require_license: bool = typer.Option(
        False,
        "--require-license",
        help="Skip candidates without license metadata (only relevant for keyed providers).",
    ),
    dedup_threshold: int = typer.Option(
        4,
        "--dedup-threshold",
        help="Max Hamming distance between dHashes for near-duplicate detection (0 disables).",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "-y",
        help="Skip all prompts; derive session from CLI args and config defaults.",
    ),
    project_name: str = typer.Option(
        "",
        "--project-name",
        help="Collection name (required in --non-interactive mode).",
    ),
    categories: str = typer.Option(
        "",
        "--categories",
        help="Comma-separated category names for non-interactive mode.",
    ),
    profile: str = typer.Option(
        "",
        "--profile",
        help="Named profile from ~/.kismet/config.toml to pre-populate session fields.",
    ),
    config_path: str = typer.Option(
        "",
        "--config",
        help="Path to a config.toml file (default: ~/.kismet/config.toml).",
    ),
    min_score: float = typer.Option(
        0.0,
        "--min-score",
        help="Minimum relevance score [0–1] to keep an image (0 = disabled, no scoring overhead).",
    ),
    resize_max_px: int = typer.Option(
        0,
        "--resize-max-px",
        help="Fit longest side to this many pixels after download (0 = off).",
    ),
    crop_aspect: str = typer.Option(
        "",
        "--crop-aspect",
        help="Centre-crop to aspect ratio after download, e.g. '16:9' or '1:1' ('' = off).",
    ),
    downscale_kb: int = typer.Option(
        0,
        "--downscale-kb",
        help="Target max file size in KB via quality binary-search — JPEG/WebP only (0 = off).",
    ),
    auto_orient: bool = typer.Option(
        True,
        "--auto-orient/--no-auto-orient",
        help="Apply EXIF orientation correction (default: on).",
    ),
    remove_bg: bool = typer.Option(
        False,
        "--remove-bg",
        help="Remove image background via rembg (requires pip install kismet[bg]).",
    ),
    export: bool = typer.Option(
        False,
        "--export",
        help="Run the export stage (web-optimised assets) after harvest.",
    ),
    export_dir: str = typer.Option(
        "",
        "--export-dir",
        help="Directory for exported assets (default: <save_dir>/export).",
    ),
    export_format: str = typer.Option(
        "webp",
        "--export-format",
        help="Output format for web exports: 'webp' or 'jpg'.",
    ),
    contact_sheet: bool = typer.Option(
        False,
        "--contact-sheet",
        help="Generate a contact-sheet PNG grid of all harvested images.",
    ),
    zip_export: bool = typer.Option(
        False,
        "--zip",
        help="Zip the export directory after export.",
    ),
    ml_dataset: bool = typer.Option(
        False,
        "--ml-dataset",
        help="Produce an ML dataset layout (train/val/test split + manifest).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be downloaded without writing any files or creating directories.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print a machine-readable JSON summary of the harvest report to stdout after the run.",
    ),
    log_file: str = typer.Option(
        "",
        "--log-file",
        help="Path to write newline-delimited JSON structured log events throughout the run.",
    ),
    plugin: list[str] = typer.Option(  # noqa: B008
        None,
        "--plugin",
        help="Dotted import path of a KismetPlugin class to load (repeatable).",
    ),
    lang: str = typer.Option(
        "",
        "--lang",
        help="Locale for CLI output: en, es, fr (default: KISMET_LANG env or system locale).",
    ),
) -> None:
    """Autonomous image harvesting agent — CLI or web UI."""
    if ctx.invoked_subcommand is None:
        try:
            init_locale(lang)
            if log_file:
                _setup_log_file(log_file)
            # (version already handled eagerly above)
            cfg_file = None if not config_path else __import__("pathlib").Path(config_path)
            kismet_cfg = load_config(cfg_file)
            scorer_name = kismet_cfg.defaults.scorer if min_score > 0.0 else ""
            active_scorer = build_scorer(scorer_name) if min_score > 0.0 else None

            # Build plugin registry: entry-point plugins first, then --plugin flags.
            active_registry = PluginRegistry()
            load_entry_points(active_registry)
            for dotted in plugin or []:
                try:
                    load_dotted_path(dotted, active_registry)
                except (ImportError, AttributeError) as exc:
                    console.print(f"[red]Error loading plugin {dotted!r}: {exc}[/red]")
                    raise typer.Exit(code=1) from exc

            active_pipeline = _make_postprocess_pipeline(
                kismet_cfg.postprocess,
                resize_max_px=resize_max_px,
                crop_aspect=crop_aspect,
                downscale_kb=downscale_kb,
                auto_orient=auto_orient,
                remove_bg=remove_bg,
            )
            if non_interactive:
                _run_session_non_interactive(
                    kismet_cfg=kismet_cfg,
                    project_name=project_name,
                    categories_raw=categories,
                    profile_name=profile,
                    resume=resume,
                    use_cache=not no_cache,
                    require_license=require_license,
                    dedup_threshold=dedup_threshold,
                    scorer=active_scorer,
                    min_score=min_score,
                    pipeline=active_pipeline,
                    do_export=export,
                    export_dir_override=export_dir,
                    export_format=export_format,
                    do_contact_sheet=contact_sheet,
                    do_zip=zip_export,
                    do_ml_dataset=ml_dataset,
                    export_defs=kismet_cfg.export,
                    json_output=json_output,
                    dry_run=dry_run,
                    plugin_registry=active_registry,
                )
            else:
                _run_session(
                    kismet_cfg=kismet_cfg,
                    profile_name=profile,
                    resume=resume,
                    use_cache=not no_cache,
                    require_license=require_license,
                    dedup_threshold=dedup_threshold,
                    scorer=active_scorer,
                    min_score=min_score,
                    pipeline=active_pipeline,
                    do_export=export,
                    export_dir_override=export_dir,
                    export_format=export_format,
                    do_contact_sheet=contact_sheet,
                    do_zip=zip_export,
                    do_ml_dataset=ml_dataset,
                    export_defs=kismet_cfg.export,
                    json_output=json_output,
                    dry_run=dry_run,
                    plugin_registry=active_registry,
                )
        except KeyboardInterrupt:
            console.print(get_text("interrupted"))
            raise typer.Exit(code=0) from None
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            if os.getenv("KISMET_DEBUG"):
                traceback.print_exc()
            else:
                console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc


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


_DISCOVERY_CACHE_DIR: Path = Path.home() / ".kismet" / "cache" / "discovery"
_STATE_FILENAME: str = ".kismet_state.json"


def _run_session(
    *,
    kismet_cfg: KismetConfig | None = None,
    profile_name: str = "",
    resume: bool = True,
    use_cache: bool = True,
    require_license: bool = False,
    dedup_threshold: int = 4,
    scorer: Scorer | None = None,
    min_score: float = 0.0,
    pipeline: PostprocessPipeline | None = None,
    do_export: bool = False,
    export_dir_override: str = "",
    export_format: str = "webp",
    do_contact_sheet: bool = False,
    do_zip: bool = False,
    do_ml_dataset: bool = False,
    export_defs: "ExportDefaults | None" = None,
    dry_run: bool = False,
    json_output: bool = False,
    plugin_registry: PluginRegistry | None = None,
) -> None:
    if kismet_cfg is None:
        kismet_cfg = KismetConfig()
    prof: ProfileConfig | None = kismet_cfg.profile(profile_name) if profile_name else None

    console.print(
        Panel.fit(
            "[bold cyan]KISMET[/bold cyan]  [dim]v0.1.0 · Universal Image Harvesting Agent[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    project_name, project_slug = _prompt_project_name()
    ai_categories, ai_result = _prompt_ai_assist(project_name, project_slug)
    collection_scope = _prompt_collection_scope()
    visual_style = _prompt_visual_style()
    exclude_keywords = _prompt_exclude_keywords()
    save_dir = _prompt_destination(project_slug)
    image_count = _prompt_image_count()
    naming_pattern, custom_prefix = _prompt_naming_pattern()

    if ai_categories is not None:
        categories = ai_categories
    elif prof and prof.categories:
        categories = [CategoryEntry(display_name=c, slug=sanitize_slug(c)) for c in prof.categories]
    else:
        categories = _prompt_categories()

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
    console.print(Rule(get_text("dir_structure_header"), style="cyan"))
    console.print(_build_tree(cfg))

    console.print(Rule(get_text("session_summary_header"), style="cyan"))
    _print_summary(cfg)

    _print_query_preview(cfg)

    if not Confirm.ask(get_text("proceed_confirm"), console=console):
        console.print(get_text("aborted"))
        raise typer.Exit(code=0)

    report = _run_download(
        cfg,
        resume=resume,
        use_cache=use_cache,
        require_license=require_license,
        dedup_threshold=dedup_threshold,
        scorer=scorer,
        min_score=min_score,
        pipeline=pipeline,
        dry_run=dry_run,
        plugin_registry=plugin_registry,
    )
    _print_download_report(report)
    if json_output:
        sys.stdout.write(_json.dumps(report.as_dict()) + "\n")
    if do_export and not dry_run:
        _run_export(
            report,
            _resolve_export_dir(cfg.save_dir, export_dir_override),
            _make_export_cfg(export_defs, export_format),
            do_thumbnails=True,
            do_contact_sheet=do_contact_sheet,
            do_zip=do_zip,
            do_ml_dataset=do_ml_dataset,
        )


def _run_session_non_interactive(
    *,
    kismet_cfg: KismetConfig,
    project_name: str,
    categories_raw: str,
    profile_name: str,
    resume: bool,
    use_cache: bool,
    require_license: bool,
    dedup_threshold: int,
    scorer: Scorer | None = None,
    min_score: float = 0.0,
    pipeline: PostprocessPipeline | None = None,
    do_export: bool = False,
    export_dir_override: str = "",
    export_format: str = "webp",
    do_contact_sheet: bool = False,
    do_zip: bool = False,
    do_ml_dataset: bool = False,
    export_defs: "ExportDefaults | None" = None,
    dry_run: bool = False,
    json_output: bool = False,
    plugin_registry: PluginRegistry | None = None,
) -> None:
    """Headless session — no Rich prompts; all values from CLI args + config."""
    prof: ProfileConfig | None = kismet_cfg.profile(profile_name) if profile_name else None
    defs = kismet_cfg.defaults

    if not project_name:
        console.print("[red]Error: --project-name is required in --non-interactive mode.[/red]")
        raise typer.Exit(code=1)

    project_slug = sanitize_slug(project_name)

    # Categories: CLI arg > profile > error
    cat_names: list[str] = []
    if categories_raw:
        cat_names = [c.strip() for c in categories_raw.split(",") if c.strip()]
    elif prof and prof.categories:
        cat_names = prof.categories

    if not cat_names:
        console.print(
            "[red]Error: --categories is required in --non-interactive mode "
            "(or use --profile with pre-defined categories).[/red]"
        )
        raise typer.Exit(code=1)

    categories = [
        CategoryEntry(display_name=c, slug=sanitize_slug(c)) for c in cat_names if sanitize_slug(c)
    ]
    if not categories:
        console.print("[red]Error: All category names produced empty slugs.[/red]")
        raise typer.Exit(code=1)

    # Resolve remaining fields: profile > config defaults > hardcoded
    collection_scope = (prof.collection_scope if prof else None) or defs.collection_scope
    visual_style = (prof.visual_style if prof else None) or defs.visual_style
    exclude_keywords = (prof.exclude_keywords if prof else None) or defs.exclude_keywords
    naming_pattern = (prof.naming_pattern if prof else None) or defs.naming_pattern
    image_count = (prof.image_count if prof else None) or defs.image_count

    raw_save_dir = (prof.save_dir if prof else None) or defs.save_dir
    if raw_save_dir:
        expanded = Path(raw_save_dir).expanduser()
        try:
            save_dir = resolve_safe_path(expanded.parent, expanded.name)
        except PermissionError as exc:
            console.print(f"[red]Error: save_dir is not writable: {exc}[/red]")
            raise typer.Exit(code=1) from exc
    else:
        default_dir = Path.home() / "Downloads" / f"kismet_{project_slug}"
        save_dir = default_dir

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
        custom_prefix=None,
    )

    console.print(f"[cyan]KISMET[/cyan] non-interactive — [bold]{project_name}[/bold]")
    _print_summary(cfg)

    report = _run_download(
        cfg,
        resume=resume,
        use_cache=use_cache,
        require_license=require_license,
        dedup_threshold=dedup_threshold,
        scorer=scorer,
        min_score=min_score,
        pipeline=pipeline,
        dry_run=dry_run,
        plugin_registry=plugin_registry,
    )
    _print_download_report(report)
    if json_output:
        sys.stdout.write(_json.dumps(report.as_dict()) + "\n")
    if do_export and not dry_run:
        _run_export(
            report,
            _resolve_export_dir(cfg.save_dir, export_dir_override),
            _make_export_cfg(export_defs, export_format),
            do_thumbnails=True,
            do_contact_sheet=do_contact_sheet,
            do_zip=do_zip,
            do_ml_dataset=do_ml_dataset,
        )
