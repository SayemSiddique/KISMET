"""FastAPI web backend for KISMET browser UI."""


import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.downloader import CategoryJob, harvest, prune_empty_dirs
from src.llm import OllamaConnectionError, brainstorm_categories
from src.utils import DDG_TYPE_MAP, STYLE_MAP, build_search_query, build_stem, resolve_safe_path, sanitize_slug


class BrainstormRequest(BaseModel):
    goal: str
    count: int = 5


class HarvestRequest(BaseModel):
    project_name: str
    save_dir: str = ""
    image_count: int = Field(default=3, ge=1, le=50)
    naming_pattern: str = "[item]_[index]"
    collection_scope: str = ""
    visual_style: str = "none"
    exclude_keywords: str = ""
    categories: list[dict[str, Any]] = Field(default_factory=list)


def build_app() -> FastAPI:
    app = FastAPI(title="KISMET", docs_url=None, redoc_url=None)

    _templates = Path(__file__).parent / "templates"
    app.mount("/static", StaticFiles(directory=str(_templates)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_templates / "index.html")

    @app.get("/api/default-path")
    async def default_path(slug: str = "") -> dict[str, str]:
        safe_slug = sanitize_slug(slug) or "kismet"
        path = Path.home() / "Downloads" / f"kismet_{safe_slug}"
        return {"path": str(path)}

    @app.post("/api/brainstorm")
    async def brainstorm_route(req: BrainstormRequest) -> JSONResponse:
        try:
            result = brainstorm_categories(goal=req.goal, category_count=req.count)
        except OllamaConnectionError as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})
        return JSONResponse(result.model_dump())

    @app.websocket("/ws/harvest")
    async def harvest_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            raw = await websocket.receive_text()
            config: dict[str, Any] = json.loads(raw)
            await _run_harvest(websocket, config)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
            except Exception:
                pass

    return app


async def _run_harvest(websocket: WebSocket, config: dict[str, Any]) -> None:
    req = HarvestRequest.model_validate(config)
    project_slug = sanitize_slug(req.project_name) or "collection"
    raw_save_dir = req.save_dir
    image_count = req.image_count
    naming_pattern = req.naming_pattern
    collection_scope = req.collection_scope.strip()
    visual_style = req.visual_style
    exclude_keywords = req.exclude_keywords.strip()
    categories_raw: list[dict[str, Any]] = req.categories

    style_suffix = STYLE_MAP.get(visual_style, "")
    ddg_filter = DDG_TYPE_MAP.get(visual_style, "")

    # Resolve save directory safely
    expanded = Path(raw_save_dir).expanduser() if raw_save_dir else (
        Path.home() / "Downloads" / f"kismet_{project_slug}"
    )
    try:
        save_dir = resolve_safe_path(expanded.parent, expanded.name)
    except PermissionError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        return

    # Build jobs
    jobs: list[CategoryJob] = []
    for cat in categories_raw:
        cat_slug = sanitize_slug(cat.get("display_name", ""))
        if not cat_slug:
            continue
        try:
            cat_dir = resolve_safe_path(save_dir, cat_slug)
        except PermissionError:
            continue
        for item in cat.get("items", []):
            item_display = item.get("display_name", "")
            item_slug = sanitize_slug(item_display)
            item_spec = item.get("specification", "").strip()
            if not item_slug:
                continue
            query = build_search_query(
                item_display=item_display,
                collection_scope=collection_scope,
                item_spec=item_spec,
                style_suffix=style_suffix,
                exclude_keywords=exclude_keywords,
            )
            stems = [
                build_stem(naming_pattern, cat_slug, item_slug, i)
                for i in range(1, image_count + 1)
            ]
            jobs.append(CategoryJob(
                folder_slug=f"{cat_slug}/{item_slug}",
                search_query=query,
                dest_dir=cat_dir,
                filenames=stems,
                image_type_filter=ddg_filter,
            ))

    if not jobs:
        await websocket.send_text(json.dumps({"type": "error", "message": "No valid jobs to run."}))
        return

    total = sum(len(j.filenames) for j in jobs)
    await websocket.send_text(json.dumps({"type": "start", "total": total}))

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_progress(event: str, folder_slug: str, detail: str) -> None:
        queue.put_nowait({"type": "progress", "event": event, "slug": folder_slug, "detail": detail})

    async def drain_queue() -> None:
        while True:
            msg = await queue.get()
            await websocket.send_text(json.dumps(msg))
            if msg.get("event") == "__done__":
                break

    harvest_task = asyncio.create_task(harvest(jobs, on_progress=on_progress))
    drain_task = asyncio.create_task(drain_queue())

    report = await harvest_task
    queue.put_nowait({"type": "progress", "event": "__done__", "slug": "", "detail": ""})
    await drain_task

    categories_report = [
        {
            "slug": cat.folder_slug,
            "saved": cat.saved_count,
            "requested": cat.requested,
            "error": cat.error,
        }
        for cat in report.categories
    ]
    await websocket.send_text(json.dumps({
        "type": "report",
        "total_saved": report.total_saved,
        "total_requested": report.total_requested,
        "save_dir": str(save_dir),
        "categories": categories_report,
    }))


