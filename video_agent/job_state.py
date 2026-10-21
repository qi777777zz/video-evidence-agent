"""Persisted business state machine for video jobs."""
from __future__ import annotations

from sqlalchemy import select

from .db import Session, StageExecution, Video

NORMAL_STAGES = {"uploaded", "validating", "processing", "transcribing",
                 "ocr_processing", "summarizing", "indexing", "ready"}
EXCEPTION_STATES = {"cancelling", "cancelled", "retrying", "failed"}
TERMINAL_STATES = {"ready", "cancelled"}

ALLOWED_TRANSITIONS = {
    "uploaded": {"validating", "cancelling", "failed"},
    "validating": {"processing", "cancelling", "failed"},
    "processing": {"transcribing", "ocr_processing", "summarizing", "indexing",
                   "ready", "cancelling", "failed"},
    "transcribing": {"processing", "ocr_processing", "summarizing", "cancelling", "failed"},
    "ocr_processing": {"processing", "transcribing", "summarizing", "cancelling", "failed"},
    "summarizing": {"processing", "indexing", "cancelling", "failed"},
    "indexing": {"processing", "ready", "cancelling", "failed"},
    "cancelling": {"cancelled", "failed"},
    "cancelled": {"retrying"},
    "failed": {"retrying", "cancelling"},
    "retrying": {"validating", "processing", "transcribing", "ocr_processing",
                 "summarizing", "indexing", "failed", "cancelling"},
    "ready": {"retrying"},
}


class InvalidStateTransition(ValueError):
    pass


def transition(video: Video, target: str, *, stage: str | None = None,
               progress: int | None = None, error: str | None = None) -> None:
    if target != video.status and target not in ALLOWED_TRANSITIONS.get(video.status, set()):
        raise InvalidStateTransition(f"illegal job transition: {video.status} -> {target}")
    video.status = target
    video.stage = stage or target
    if progress is not None:
        video.progress = max(0, min(100, int(progress)))
    video.error = error


def stage_snapshot(video_id: str, stage: str, input_version: str, status: str,
                   *, output_version: str | None = None, progress: int = 0,
                   error: str | None = None, output_data=None,
                   increment_attempt: bool = False) -> StageExecution:
    with Session.begin() as session:
        row = session.scalar(select(StageExecution).where(
            StageExecution.video_id == video_id, StageExecution.stage == stage,
            StageExecution.input_version == input_version))
        if row is None:
            row = StageExecution(video_id=video_id, stage=stage, input_version=input_version)
            session.add(row)
        row.status, row.progress, row.error = status, max(0, min(100, progress)), error
        if output_version is not None:
            row.output_version = output_version
        if output_data is not None:
            row.output_data = output_data
        if increment_attempt:
            row.attempts += 1
        session.flush()
        return row


def successful_stage(video_id: str, stage: str, input_version: str) -> StageExecution | None:
    with Session() as session:
        return session.scalar(select(StageExecution).where(
            StageExecution.video_id == video_id, StageExecution.stage == stage,
            StageExecution.input_version == input_version, StageExecution.status == "succeeded"))
