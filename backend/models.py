from pydantic import BaseModel


class ParseRequest(BaseModel):
    channel: str


class ParseDMRequest(BaseModel):
    dialog: str


class AuthPhoneRequest(BaseModel):
    phone: str


class AuthCodeRequest(BaseModel):
    code: str


class AuthPasswordRequest(BaseModel):
    password: str


class ParseStats(BaseModel):
    channel_id: str = ""
    channel_username: str = ""
    channel_title: str = ""
    total_posts: int = 0
    saved_posts: int = 0
    skipped_posts: int = 0
    photos_count: int = 0
    videos_count: int = 0
    voice_count: int = 0
    files_count: int = 0
    links_count: int = 0
    # DM parsing only: voice messages and round video notes turned into text
    transcribed_count: int = 0
    # Seconds of speech found vs. seconds already recognised — drives the
    # transcription progress bar, which moves very differently from the
    # message counter (one 5-minute voice note outweighs 200 texts).
    voice_seconds: int = 0
    transcribed_seconds: int = 0


class ProgressMessage(BaseModel):
    current: int
    total: int
    done: bool = False
    error: str | None = None
    cancelled: bool = False
    stats: ParseStats
    # What the backend is doing right now, so the UI can explain a long pause:
    # "loading_model" | "scanning" | "downloading" | "transcribing"
    stage: str | None = None
    # Human-readable detail for the current item, e.g. "кружок 0:08"
    detail: str | None = None


class ParseResponse(BaseModel):
    task_id: str


class HistoryRecord(BaseModel):
    id: int
    created_at: str
    channel_id: str
    channel_username: str | None
    channel_title: str | None
    total_posts: int
    saved_posts: int
    skipped_posts: int
    photos_count: int
    videos_count: int
    voice_count: int
    files_count: int
    links_count: int
    transcribed_count: int = 0
    status: str
    error_message: str | None = None
