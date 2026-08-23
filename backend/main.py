import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse

from . import database, exporter, parser
from .models import (
    HistoryRecord,
    ParseRequest,
    ParseResponse,
    ParseStats,
    ProgressMessage,
)

# Configure logging once at import time so it applies under uvicorn reload
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)-12s | %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger("app")


@dataclass
class TaskState:
    task: asyncio.Task
    stats: ParseStats = field(default_factory=ParseStats)
    current: int = 0
    total: int = 0
    done: bool = False
    error: str | None = None
    waiters: list[asyncio.Queue] = field(default_factory=list)


_tasks: dict[str, TaskState] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    yield


app = FastAPI(title="Telegram Parser", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    """Backend serves only the API; the UI lives on the Vite dev server."""
    return RedirectResponse("/docs")


@app.post("/api/parse", response_model=ParseResponse)
async def start_parse(req: ParseRequest):
    task_id = str(uuid.uuid4())
    state = TaskState(task=None)  # type: ignore[arg-type]
    _tasks[task_id] = state
    logger.info("[task %s] received parse request: channel=%r", task_id[:8], req.channel)

    async def progress_cb(current: int, total: int, stats: ParseStats) -> None:
        state.current = current
        state.total = total
        state.stats = stats
        msg = ProgressMessage(current=current, total=total, stats=stats)
        for q in list(state.waiters):
            await q.put(msg.model_dump())

    async def run() -> None:
        record_id = await database.create_record(
            channel_id="",
            channel_username="",
            channel_title="",
        )
        logger.info("[task %s] db record created: id=%s", task_id[:8], record_id)
        try:
            result = await parser.parse_channel(req.channel, progress_cb, state.stats)
            await database.update_record(record_id, "completed", result.model_dump())
            state.done = True
            logger.info(
                "[task %s] COMPLETED: channel=%r posts=%s",
                task_id[:8], result.channel_title or result.channel_id, result.saved_posts,
            )
            msg = ProgressMessage(
                current=state.current,
                total=state.total,
                done=True,
                stats=result,
            )
            for q in list(state.waiters):
                await q.put(msg.model_dump())
        except Exception as exc:
            error_msg = str(exc) or exc.__class__.__name__
            state.error = error_msg
            state.done = True
            logger.exception("[task %s] FAILED: %s", task_id[:8], error_msg)
            await database.update_record(
                record_id, "failed", state.stats.model_dump(), error_message=error_msg,
            )
            for q in list(state.waiters):
                await q.put({"error": error_msg, "done": True})

    state.task = asyncio.create_task(run())
    return ParseResponse(task_id=task_id)


@app.websocket("/ws/progress/{task_id}")
async def ws_progress(websocket: WebSocket, task_id: str):
    await websocket.accept()

    state = _tasks.get(task_id)
    if state is None:
        await websocket.send_json({"error": "task not found"})
        await websocket.close()
        return

    queue: asyncio.Queue[Any] = asyncio.Queue()
    state.waiters.append(queue)

    # send current snapshot immediately
    await websocket.send_json(
        ProgressMessage(
            current=state.current,
            total=state.total,
            done=state.done,
            error=state.error,
            stats=state.stats,
        ).model_dump()
    )

    try:
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
            if msg.get("done"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        state.waiters.remove(queue)


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    state = _tasks.get(task_id)
    if state is None:
        return {"error": "task not found"}
    return ProgressMessage(
        current=state.current,
        total=state.total,
        done=state.done,
        error=state.error,
        stats=state.stats,
    ).model_dump()


@app.get("/api/history", response_model=list[HistoryRecord])
async def get_history():
    rows = await database.get_history()
    return rows


@app.get("/api/channels")
async def get_channels():
    return exporter.list_channels()


@app.get("/api/channels/{channel_id}/export")
async def export_channel(channel_id: str, transcribe: bool = False):
    try:
        text = await exporter.export_channel_text(channel_id, transcribe=transcribe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    username = channel_id
    for ch in exporter.list_channels():
        if ch["channel_id"] == channel_id:
            username = ch["username"] or channel_id
            break

    filename = f"{username}_export.txt"
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
