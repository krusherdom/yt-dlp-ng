"""Pydantic request/response models."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

STATUSES = ("queued", "running", "done", "failed", "cancelled")
JOB_TYPES = ("single", "playlist", "child")


class JobCreate(BaseModel):
    url: str
    preset: str = "best"
    subfolder: Optional[str] = ""
    extra_args: Optional[str] = ""


class BulkJobCreate(BaseModel):
    urls: List[str] = Field(default_factory=list)
    preset: str = "best"
    subfolder: Optional[str] = ""
    extra_args: Optional[str] = ""


class Job(BaseModel):
    id: str
    url: str
    title: Optional[str] = None
    preset: str = "best"
    subfolder: str = ""
    extra_args: str = ""
    status: str = "queued"
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    parent_id: Optional[str] = None
    type: str = "single"
    created_at: str
    updated_at: str


class ImportRequest(BaseModel):
    text: Optional[str] = ""


class ImportCandidate(BaseModel):
    url: str
    extractor: str


class ImportResult(BaseModel):
    candidates: List[ImportCandidate] = Field(default_factory=list)
    rejected: List[str] = Field(default_factory=list)
    total_found: int = 0


class FolderCreate(BaseModel):
    path: str


class StatusResponse(BaseModel):
    ytdlp_version: str
    cookies_detected: bool
    max_concurrent: int
    downloads_root: str
