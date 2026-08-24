import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from . import auth, database, dialogs, exporter, parser, transcriber
from .models import (
    AuthCodeRequest,
    AuthPasswordRequest,
    AuthPhoneRequest,
    HistoryRecord,
    ParseDMRequest,
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

# The login endpoints accept a phone code and a cloud password, so the API is
# no longer safe to expose to any origin: a page in another tab could otherwise
# drive the login flow. Restricted to the local dev servers that need it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    """Backend serves only the API; the UI lives on the Vite dev server."""
    return RedirectResponse("/docs")


# ---------------------------------------------------------------------------
# Telegram login
# ---------------------------------------------------------------------------

@app.get("/api/auth/status")
async def auth_status():
    """Whether the stored session is usable, and as whom."""
    try:
        return await auth.status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось проверить сессию: {exc}")


@app.post("/api/auth/send-code")
async def auth_send_code(req: AuthPhoneRequest):
    """Ask Telegram to send a login code."""
    try:
        return await auth.send_code(req.phone)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/sign-in")
async def auth_sign_in(req: AuthCodeRequest):
    """Finish the login with the code from Telegram."""
    try:
        return await auth.sign_in(req.code)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/password")
async def auth_password(req: AuthPasswordRequest):
    """Second factor for accounts with a cloud password."""
    try:
        return await auth.sign_in_password(req.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/cancel")
async def auth_cancel():
    """Abandon a login in progress."""
    return await auth.cancel()


@app.post("/api/auth/logout")
async def auth_logout():
    """Log out and invalidate the stored session."""
    try:
        return await auth.log_out()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось выйти: {exc}")


def _start_task(kind: str, target: str, run_parser) -> str:
    """
    Register a background parse task and wire it to progress/history plumbing.

    `run_parser(progress_cb, stats)` performs the actual work; channels and
    private chats differ only in that callable.
    """
    task_id = str(uuid.uuid4())
    state = TaskState(task=None)  # type: ignore[arg-type]
    _tasks[task_id] = state
    logger.info("[task %s] received %s request: %r", task_id[:8], kind, target)

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
            result = await run_parser(progress_cb, state.stats)
            await database.update_record(record_id, "completed", result.model_dump())
            state.done = True
            logger.info(
                "[task %s] COMPLETED: %r saved=%s transcribed=%s",
                task_id[:8], result.channel_title or result.channel_id,
                result.saved_posts, result.transcribed_count,
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
    return task_id


@app.post("/api/parse", response_model=ParseResponse)
async def start_parse(req: ParseRequest):
    task_id = _start_task(
        "channel parse",
        req.channel,
        lambda cb, stats: parser.parse_channel(req.channel, cb, stats),
    )
    return ParseResponse(task_id=task_id)


@app.post("/api/parse-dm", response_model=ParseResponse)
async def start_parse_dm(req: ParseDMRequest):
    """Parse a private chat, turning voice messages and round videos into text."""
    task_id = _start_task(
        "dm parse",
        req.dialog,
        lambda cb, stats: dialogs.parse_dialog(req.dialog, cb, stats),
    )
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


# ---------------------------------------------------------------------------
# Private messages
# ---------------------------------------------------------------------------

@app.get("/api/dialogs")
async def get_telegram_dialogs(limit: int = 100):
    """Private chats on the account, offered as parse targets."""
    try:
        return await dialogs.list_telegram_dialogs(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить список диалогов: {exc}")


@app.get("/api/dms")
async def get_parsed_dialogs():
    """Private chats already parsed to disk."""
    return dialogs.list_parsed_dialogs()


@app.get("/api/asr-model")
async def get_asr_model():
    """Which local speech-recognition model this backend is configured to use."""
    return transcriber.model_info()


@app.get("/api/dms/{dialog_id}/export")
async def export_dialog(dialog_id: str):
    try:
        chunks = exporter.iter_dialog_export(dialog_id)
        first = next(chunks)  # surfaces a missing dialog before streaming starts
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    def stream():
        yield first
        yield from chunks

    filename = f"dm_{dialog_id}_export.txt"
    return StreamingResponse(
        stream(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
