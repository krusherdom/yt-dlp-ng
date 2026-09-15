"""Pydantic request/response models."""

from __future__ import annotations

import uuid
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from . import config

STATUSES = ("queued", "running", "done", "failed", "cancelled")
JOB_TYPES = ("single", "playlist", "child")

#: Proxy URL schemes yt-dlp can route a download through natively.
PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


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
    proxy: Optional[str] = None
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


# --------------------------------------------------------------------------
# Proxy pool
# --------------------------------------------------------------------------


def normalise_domain(raw: str) -> str:
    """``https://WWW.Example.com:443/path`` -> ``example.com``.

    Returns "" for anything that does not leave a hostname behind, so callers
    can simply drop the falsy results.
    """
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "//" in value:
        value = value.split("//", 1)[1]
    # Strip credentials, path/query/fragment and any port.
    value = value.split("@")[-1]
    for sep in ("/", "?", "#"):
        value = value.split(sep, 1)[0]
    if value.startswith("["):  # bracketed IPv6 literal
        value = value.split("]", 1)[0].lstrip("[")
    elif ":" in value:
        value = value.split(":", 1)[0]
    value = value.strip().strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


class ProxyEntry(BaseModel):
    """One configured proxy. ``url`` may embed credentials."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    url: str
    label: str = ""
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            raise ValueError("Proxy URL is required")
        if any(ch.isspace() for ch in raw):
            raise ValueError("Proxy URL must not contain whitespace")
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in PROXY_SCHEMES:
            raise ValueError(
                f"Unsupported proxy scheme '{parts.scheme or raw}'; "
                f"use one of {', '.join(PROXY_SCHEMES)}"
            )
        try:
            host = parts.hostname
            port = parts.port
        except ValueError as exc:  # non-numeric / out-of-range port
            raise ValueError(f"Invalid proxy port: {exc}") from exc
        if not host:
            raise ValueError("Proxy URL needs a host")
        if port is None:
            raise ValueError("Proxy URL needs an explicit port, e.g. http://10.0.0.5:8888")
        return raw

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        return (value or "").strip()


class ProxySettings(BaseModel):
    """The whole persisted proxy configuration (``/config/settings.json``)."""

    proxies: List[ProxyEntry] = Field(default_factory=list)
    mode: Literal["domains", "all"] = "domains"
    domains: List[str] = Field(default_factory=list)
    test_url: str = Field(default_factory=lambda: config.PROXY_TEST_URL)
    check_interval_s: int = Field(default=300, ge=30, le=86400)
    timeout_s: int = Field(default=8, ge=2, le=60)

    @field_validator("domains")
    @classmethod
    def _clean_domains(cls, value: List[str]) -> List[str]:
        out: List[str] = []
        for raw in value or []:
            domain = normalise_domain(raw)
            if domain and domain not in out:
                out.append(domain)
        return out

    @field_validator("test_url")
    @classmethod
    def _clean_test_url(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return config.PROXY_TEST_URL
        if urlsplit(raw).scheme.lower() not in ("http", "https"):
            raise ValueError("Test URL must be http:// or https://")
        return raw

    @field_validator("proxies")
    @classmethod
    def _no_duplicates(cls, value: List[ProxyEntry]) -> List[ProxyEntry]:
        seen_urls: set = set()
        seen_ids: set = set()
        for entry in value or []:
            if entry.url in seen_urls:
                raise ValueError(f"Duplicate proxy URL: {entry.url}")
            seen_urls.add(entry.url)
            if entry.id in seen_ids:
                raise ValueError(f"Duplicate proxy id: {entry.id}")
            seen_ids.add(entry.id)
        return list(value or [])


class ProxyStatus(BaseModel):
    """Live health of one proxy. ``live is None`` means "never checked"."""

    id: str
    live: Optional[bool] = None
    latency_ms: Optional[int] = None
    last_checked: Optional[str] = None
    last_error: Optional[str] = None
    exit_ip: Optional[str] = None
