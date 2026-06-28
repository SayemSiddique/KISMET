"""FastAPI web backend for KISMET browser UI."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import load_discovery_config
from src.downloader import CategoryJob, build_provider, harvest
from src.export import ExportConfig, export_contact_sheet, export_ml_dataset, export_web, export_zip
from src.llm import OllamaConnectionError, brainstorm_categories
from src.postprocess import PostprocessConfig, PostprocessPipeline
from src.utils import (
    DDG_TYPE_MAP,
    STYLE_MAP,
    build_search_query,
    build_stem,
    resolve_safe_path,
    sanitize_slug,
)

_SESSIONS_DIR = Path.home() / ".kismet" / "sessions"


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
    # Advanced options
    provider_order: list[str] = Field(default_factory=list)
    require_license: bool = False
    dedup_threshold: int = Field(default=4, ge=0, le=20)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    # Post-processing
    postprocess_resize: int = Field(default=0, ge=0)
    postprocess_remove_bg: bool = False
    # Export
    export_mode: str = "none"  # "none" | "zip" | "web" | "ml"
    export_format: str = "webp"  # "webp" | "jpg"


class QueryPatchRequest(BaseModel):
    query: str


def build_app() -> FastAPI:
    app = FastAPI(title="KISMET", docs_url=None, redoc_url=None)

    _templates = Path(__file__).parent / "templates"
    app.mount("/static", StaticFiles(directory=str(_templates)), name="static")

    # --- in-memory session state (scoped to this app instance) ---
    _image_states: dict[str, str] = {}  # image_id → "accepted"|"rejected"|"pending"
    _active_jobs: list[CategoryJob] = []  # current harvest jobs
    _active_request: list[HarvestRequest] = [None]  # type: ignore[list-item]
    _last_report: list[dict[str, Any] | None] = [None]
    _rerun_tasks: list[asyncio.Task[Any]] = []  # background rerun tasks

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_templates / "index.html")

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        from src.config import ALL_PROVIDERS, KEYED_PROVIDERS, PROVIDER_KEY_ENV

        available = []
        for p in ALL_PROVIDERS:
            env_var = PROVIDER_KEY_ENV.get(p, "")
            has_key = bool(env_var and __import__("os").environ.get(env_var))
            available.append({
                "id": p,
                "keyed": p in KEYED_PROVIDERS,
                "available": has_key or p not in KEYED_PROVIDERS,
            })
        return {"providers": available}

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

    # ------------------------------------------------------------------ #
    #  Per-image accept / reject
    # ------------------------------------------------------------------ #

    @app.post("/api/images/{image_id}/accept")
    async def accept_image(image_id: str) -> dict[str, str]:
        if image_id not in _image_states:
            raise HTTPException(status_code=404, detail=f"Image '{image_id}' not found")
        _image_states[image_id] = "accepted"
        return {"id": image_id, "state": "accepted"}

    @app.post("/api/images/{image_id}/reject")
    async def reject_image(image_id: str) -> dict[str, str]:
        if image_id not in _image_states:
            raise HTTPException(status_code=404, detail=f"Image '{image_id}' not found")
        _image_states[image_id] = "rejected"
        return {"id": image_id, "state": "rejected"}

    @app.get("/api/images/{image_id}/state")
    async def get_image_state(image_id: str) -> dict[str, str]:
        if image_id not in _image_states:
            raise HTTPException(status_code=404, detail=f"Image '{image_id}' not found")
        return {"id": image_id, "state": _image_states[image_id]}

    @app.post("/api/images/{image_id}/register")
    async def register_image(image_id: str) -> dict[str, str]:
        """Register an image as pending (called by the UI when an image is displayed)."""
        if image_id not in _image_states:
            _image_states[image_id] = "pending"
        return {"id": image_id, "state": _image_states[image_id]}

    # ------------------------------------------------------------------ #
    #  Inline query edit
    # ------------------------------------------------------------------ #

    @app.patch("/api/jobs/{job_id:path}/query")
    async def patch_job_query(job_id: str, body: QueryPatchRequest) -> dict[str, Any]:
        """Update the search_query of an active job (job_id == folder_slug)."""
        for job in _active_jobs:
            if job.folder_slug == job_id:
                job.search_query = body.query
                return {
                    "folder_slug": job.folder_slug,
                    "search_query": job.search_query,
                    "dest_dir": str(job.dest_dir),
                    "filenames": job.filenames,
                }
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # ------------------------------------------------------------------ #
    #  Per-category re-run
    # ------------------------------------------------------------------ #

    @app.post("/api/categories/{slug}/rerun")
    async def rerun_category(slug: str, background_tasks: BackgroundTasks) -> dict[str, str]:
        """Re-dispatch harvest for all jobs whose folder_slug starts with slug/."""
        matching = [
            j for j in _active_jobs if j.folder_slug == slug or j.folder_slug.startswith(f"{slug}/")
        ]
        if not matching:
            raise HTTPException(status_code=404, detail=f"Category '{slug}' not found")

        async def _do_rerun(jobs: list[CategoryJob]) -> None:
            req = _active_request[0]
            disc_cfg = load_discovery_config()
            if req and req.provider_order:
                disc_cfg.order = req.provider_order
            _provider = build_provider(disc_cfg)
            if req:
                _pp_cfg = PostprocessConfig(
                    resize_max_px=req.postprocess_resize,
                    remove_bg=req.postprocess_remove_bg,
                    auto_orient=True,
                )
                _pipeline: PostprocessPipeline | None = (
                    None if PostprocessPipeline(_pp_cfg).is_noop() else PostprocessPipeline(_pp_cfg)
                )
                for j in jobs:
                    j.postprocess = _pipeline
            await harvest(
                jobs,
                provider=_provider,
                require_license=req.require_license if req else False,
                dedup_threshold=req.dedup_threshold if req else 4,
            )

        background_tasks.add_task(_do_rerun, matching)
        return {"status": "rerunning", "slug": slug}

    # ------------------------------------------------------------------ #
    #  Session persistence
    # ------------------------------------------------------------------ #

    @app.post("/api/session/save")
    async def save_session() -> dict[str, Any]:
        req = _active_request[0]
        if req is None:
            raise HTTPException(status_code=400, detail="No active harvest session")

        project_slug = sanitize_slug(req.project_name) or "collection"
        ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        session_id = f"{ts}_{project_slug}"

        results: dict[str, int] = {}
        report = _last_report[0]
        if report:
            for cat in report.get("categories", []):
                results[cat["slug"]] = cat["saved"]

        session_data: dict[str, Any] = {
            "id": session_id,
            "project_name": req.project_name,
            "categories": req.categories,
            "image_count": req.image_count,
            "save_dir": req.save_dir,
            "results": results,
            "saved_at": datetime.now(tz=UTC).isoformat(),
        }

        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session_file = _SESSIONS_DIR / f"{session_id}.json"
        session_file.write_text(json.dumps(session_data, indent=2))

        return session_data

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        if not _SESSIONS_DIR.exists():
            return {"sessions": []}
        sessions = []
        for f in sorted(_SESSIONS_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                sessions.append(
                    {
                        "id": data.get("id", f.stem),
                        "project_name": data.get("project_name", ""),
                        "saved_at": data.get("saved_at", ""),
                        "image_count": data.get("image_count", 0),
                    }
                )
            except Exception:
                continue
        return {"sessions": sessions}

    @app.get("/api/sessions/{session_id}")
    async def load_session(session_id: str) -> dict[str, Any]:
        session_file = _SESSIONS_DIR / f"{session_id}.json"
        if not session_file.exists():
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        try:
            return json.loads(session_file.read_text())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ------------------------------------------------------------------ #
    #  WebSocket harvest
    # ------------------------------------------------------------------ #

    @app.websocket("/ws/harvest")
    async def harvest_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            raw = await websocket.receive_text()
            config: dict[str, Any] = json.loads(raw)
            await _run_harvest(
                websocket, config, _active_jobs, _active_request, _last_report, _image_states
            )
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            import contextlib

            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))

    return app


async def _run_harvest(
    websocket: WebSocket,
    config: dict[str, Any],
    active_jobs: list[CategoryJob],
    active_request: list[Any],
    last_report: list[Any],
    image_states: dict[str, str],
) -> None:
    req = HarvestRequest.model_validate(config)
    active_request[0] = req

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

    # Build post-processing pipeline if any option is enabled
    pp_cfg = PostprocessConfig(
        resize_max_px=req.postprocess_resize,
        remove_bg=req.postprocess_remove_bg,
        auto_orient=True,
    )
    pipeline = None if PostprocessPipeline(pp_cfg).is_noop() else PostprocessPipeline(pp_cfg)

    expanded = (
        Path(raw_save_dir).expanduser()
        if raw_save_dir
        else (Path.home() / "Downloads" / f"kismet_{project_slug}")
    )
    try:
        save_dir = resolve_safe_path(expanded.parent, expanded.name)
    except PermissionError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        return

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
            job = CategoryJob(
                folder_slug=f"{cat_slug}/{item_slug}",
                search_query=query,
                dest_dir=cat_dir,
                filenames=stems,
                image_type_filter=ddg_filter,
                min_score=req.min_score,
                postprocess=pipeline,
            )
            jobs.append(job)
            # Register each stem as a pending image
            for stem in stems:
                if stem not in image_states:
                    image_states[stem] = "pending"

    active_jobs.clear()
    active_jobs.extend(jobs)

    if not jobs:
        await websocket.send_text(json.dumps({"type": "error", "message": "No valid jobs to run."}))
        return

    total = sum(len(j.filenames) for j in jobs)
    await websocket.send_text(json.dumps({"type": "start", "total": total}))

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_progress(event: str, folder_slug: str, detail: str) -> None:
        queue.put_nowait(
            {"type": "progress", "event": event, "slug": folder_slug, "detail": detail}
        )

    async def drain_queue() -> None:
        while True:
            msg = await queue.get()
            await websocket.send_text(json.dumps(msg))
            if msg.get("event") == "__done__":
                break

    disc_cfg = load_discovery_config()
    if req.provider_order:
        disc_cfg.order = req.provider_order
    provider = build_provider(disc_cfg)
    harvest_task = asyncio.create_task(
        harvest(
            jobs,
            provider=provider,
            on_progress=on_progress,
            require_license=req.require_license,
            dedup_threshold=req.dedup_threshold,
        )
    )
    drain_task = asyncio.create_task(drain_queue())

    try:
        report = await harvest_task
    except Exception:
        drain_task.cancel()
        raise
    finally:
        queue.put_nowait({"type": "progress", "event": "__done__", "slug": "", "detail": ""})
    await drain_task

    # Run export stage if requested
    export_dir: Path | None = None
    if req.export_mode != "none" and report.total_saved > 0:
        export_dir = save_dir / "_export"
        exp_cfg = ExportConfig(output_format=req.export_format)
        try:
            if req.export_mode in ("web", "zip", "ml"):
                await asyncio.to_thread(export_web, report, export_dir, exp_cfg)
                await asyncio.to_thread(export_contact_sheet, report, export_dir, exp_cfg)
            if req.export_mode == "zip":
                zip_path = export_dir / f"{project_slug}_export.zip"
                await asyncio.to_thread(export_zip, export_dir, zip_path)
            if req.export_mode == "ml":
                await asyncio.to_thread(export_ml_dataset, report, export_dir, exp_cfg)
        except Exception as exc:
            await websocket.send_text(
                json.dumps({"type": "warning", "message": f"Export error: {exc}"})
            )

    categories_report = [
        {
            "slug": cat.folder_slug,
            "saved": cat.saved_count,
            "requested": cat.requested,
            "error": cat.error,
        }
        for cat in report.categories
    ]
    report_payload = {
        "type": "report",
        "total_saved": report.total_saved,
        "total_requested": report.total_requested,
        "save_dir": str(save_dir),
        "export_dir": str(export_dir) if export_dir else None,
        "categories": categories_report,
        "provider_hit_rate": report.provider_hit_rate,
        "license_breakdown": report.license_breakdown,
    }
    last_report[0] = report_payload
    await websocket.send_text(json.dumps(report_payload))
