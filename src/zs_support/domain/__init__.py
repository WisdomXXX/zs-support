"""Shared models for CLI and future GUI clients."""
from dataclasses import dataclass, field, asdict
from typing import Callable
from threading import Event


class SupportError(Exception):
    def __init__(self, message, code=1, reason="operation_failed", details=None):
        super().__init__(message)
        self.code, self.reason, self.details = code, reason, details or {}

    def to_dict(self):
        return {"message": str(self), "code": self.code, "reason": self.reason,
                "details": self.details}


@dataclass
class Endpoint:
    product: str
    board: str
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 22


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass
class Progress:
    phase: str
    bytes_done: int = 0
    bytes_total: int = 0
    bytes_per_second: float = 0
    destination: str = ""
    message: str = ""


class TaskControl:
    def __init__(self, progress: Callable[[Progress], None] = lambda event: None):
        self.cancelled = Event()
        self.progress = progress

    def check(self):
        if self.cancelled.is_set():
            raise SupportError("操作已取消，未完成文件保留供续传", 1, "cancelled")


@dataclass
class VersionEvidence:
    source: str
    status: str
    value: str = ""
    error: str = ""

    def to_dict(self):
        return asdict(self)
