"""应用层处理契约；不包含 Qt 对象或业务格式规则。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Iterable


class LogLevel(IntEnum):
    INFO = 0
    OK = 1
    WARN = 2
    ERROR = 3


LOG_INFO = LogLevel.INFO
LOG_OK = LogLevel.OK
LOG_WARN = LogLevel.WARN
LOG_ERROR = LogLevel.ERROR


@dataclass(frozen=True)
class ProcessStep:
    command: tuple[str, ...]
    timeout_seconds: float = 900.0
    capture_stdout: bool = False
    output_path: Path | None = None
    failure_warning: str | None = None
    fallback_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class FilePlan:
    source_path: Path
    output_path: Path
    temp_path: Path
    command: tuple[str, ...]
    overwrite: bool = True
    backup: bool = False
    replaces_source: bool = False
    converted: bool = False
    warnings: tuple[str, ...] = ()
    preparation_steps: tuple[ProcessStep, ...] = ()
    auxiliary_paths: tuple[Path, ...] = ()
    timeout_seconds: float = 900.0
    error: str | None = None
    skip_reason: str | None = None
    success_message: str = ""

    @property
    def steps(self) -> tuple[ProcessStep, ...]:
        return (*self.preparation_steps, ProcessStep(self.command, self.timeout_seconds))


@dataclass(frozen=True)
class BatchPlan:
    """批次也是“完成当前批次后结束”的边界。"""

    name: str
    files: tuple[FilePlan, ...]
    log_events: tuple[LogEvent, ...] = ()


@dataclass(frozen=True)
class TaskProgress:
    done: int
    total: int
    current_file: str


@dataclass(frozen=True)
class LogEvent:
    level: LogLevel
    message: str


@dataclass(frozen=True)
class TaskPlan:
    batches: tuple[BatchPlan, ...]
    log_events: tuple[LogEvent, ...] = ()
    skipped_dirs: int = 0
    output_dirs: tuple[Path, ...] = ()

    @property
    def total(self) -> int:
        return sum(len(batch.files) for batch in self.batches)


@dataclass(frozen=True)
class TaskStatistics:
    total: int
    ok: int
    failed: int
    skipped: int
    done: int


@dataclass(frozen=True)
class ProcessResult:
    return_code: int
    stderr: str = ""
    cancelled: bool = False
    timed_out: bool = False
    stdout: bytes = b""

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and not self.cancelled and not self.timed_out


@dataclass
class TaskResult:
    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False
    early_stopped: bool = False
    output_dirs: list[str] = field(default_factory=list)
    error_logs: list[str] = field(default_factory=list)
    skipped_logs: list[str] = field(default_factory=list)
    skipped_dirs: int = 0

    @property
    def output_dir(self) -> str:
        return self.output_dirs[0] if self.output_dirs else ""

    @classmethod
    def merge(cls, results: Iterable[TaskResult]) -> TaskResult:
        merged = cls()
        for result in results:
            merged.total += result.total
            merged.ok += result.ok
            merged.failed += result.failed
            merged.skipped += result.skipped
            merged.skipped_dirs += result.skipped_dirs
            merged.cancelled |= result.cancelled
            merged.early_stopped |= result.early_stopped
            merged.error_logs.extend(result.error_logs)
            merged.skipped_logs.extend(result.skipped_logs)
            for directory in result.output_dirs:
                if directory not in merged.output_dirs:
                    merged.output_dirs.append(directory)
        return merged
