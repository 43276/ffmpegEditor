"""不依赖 Qt 的批次执行、停止控制、进度与文件保存。"""
from __future__ import annotations

from threading import Event
from typing import Callable

from . import converter
from .output_files import commit_output, output_transaction
from .task_models import (
    FilePlan, LogEvent, LogLevel, ProcessResult, TaskPlan, TaskProgress,
    TaskResult, TaskStatistics,
)


class TaskRunner:
    def __init__(
        self, *, execute: Callable[..., ProcessResult] | None = None,
        on_log: Callable[[LogEvent], None] | None = None,
        on_progress: Callable[[TaskProgress], None] | None = None,
        on_statistics: Callable[[TaskStatistics], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        finish_check: Callable[[], bool] | None = None,
    ) -> None:
        self._execute = execute
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_statistics = on_statistics
        self._cancel = Event()
        self._finish = Event()
        self._cancel_check = cancel_check
        self._finish_check = finish_check

    def request_cancel(self) -> None:
        self._cancel.set()

    def request_finish_after_current_batch(self) -> None:
        self._finish.set()

    def _cancel_requested(self) -> bool:
        return self._cancel.is_set() or (self._cancel_check is not None and self._cancel_check())

    def _finish_requested(self) -> bool:
        return self._finish.is_set() or (self._finish_check is not None and self._finish_check())

    def _log(self, level: LogLevel, message: str) -> None:
        if self._on_log is not None:
            self._on_log(LogEvent(level, message))

    def _failed(self, result: TaskResult, message: str) -> str:
        result.error_logs.append(message)
        self._log(LogLevel.ERROR, message)
        return "failed"

    def _skipped(self, result: TaskResult, message: str) -> str:
        result.skipped_logs.append(message)
        self._log(LogLevel.WARN, message)
        return "skipped"

    def _run_file(self, plan: FilePlan, result: TaskResult) -> str:
        source = plan.source_path.name
        if plan.skip_reason is not None:
            return self._skipped(result, f"{source}：{plan.skip_reason}")
        for warning in plan.warnings:
            self._log(LogLevel.WARN, f"{source}：{warning}")
        exists_message = f"已跳过（输出文件已存在）：{plan.output_path.name}"
        if not plan.replaces_source and not plan.overwrite and plan.output_path.exists():
            directory = str(plan.output_path.parent)
            if directory not in result.output_dirs:
                result.output_dirs.append(directory)
            return self._skipped(result, exists_message)
        if plan.error is not None:
            return self._failed(result, f"{source}：{plan.error}")

        try:
            with output_transaction(plan):
                directory = str(plan.output_path.parent)
                if directory not in result.output_dirs:
                    result.output_dirs.append(directory)
                command = plan.command
                execute = self._execute or converter.execute
                for step in plan.preparation_steps:
                    if self._cancel_requested():
                        return "cancelled"
                    process = execute(
                        step.command, cancel_check=self._cancel_requested,
                        timeout_seconds=step.timeout_seconds, capture_stdout=step.capture_stdout,
                    )
                    if process.cancelled or self._cancel_requested():
                        return "cancelled"
                    output_missing = step.output_path is not None and not step.output_path.is_file()
                    if not process.succeeded or output_missing:
                        detail = process.stderr or ("未生成准备步骤的输出" if output_missing else "命令执行失败")
                        if step.failure_warning is None:
                            return self._failed(result, f"{source} 处理失败：{detail}")
                        self._log(LogLevel.WARN, f"{source}：{step.failure_warning}（{detail}）")
                        if step.fallback_command is not None:
                            command = step.fallback_command
                if self._cancel_requested():
                    return "cancelled"
                process = execute(command, cancel_check=self._cancel_requested,
                                  timeout_seconds=plan.timeout_seconds)
                if process.cancelled or self._cancel_requested():
                    return "cancelled"
                if not process.succeeded:
                    return self._failed(result, f"{source} 处理失败：{process.stderr or '命令执行失败'}")
                if not commit_output(plan):
                    return self._skipped(result, exists_message)
                if plan.replaces_source and plan.backup:
                    self._log(LogLevel.INFO, f"{source}：已备份到 {source}.bak")
                self._log(LogLevel.OK, plan.success_message or f"{source} → {plan.output_path.name} 完成")
                return "ok"
        except Exception as exc:  # 清理本文件后继续，保证每项都有结果。
            return self._failed(result, f"{source}：{exc}")

    def _statistics(self, result: TaskResult) -> None:
        if self._on_statistics is not None:
            self._on_statistics(TaskStatistics(
                result.total, result.ok, result.failed, result.skipped,
                result.ok + result.failed + result.skipped,
            ))

    def run(self, plan: TaskPlan) -> TaskResult:
        result = TaskResult(total=plan.total, skipped_dirs=plan.skipped_dirs,
                            output_dirs=[str(directory) for directory in plan.output_dirs])
        for event in plan.log_events:
            self._log(event.level, event.message)
        self._log(LogLevel.INFO, f"任务开始：{len(plan.batches)} 个批次、{plan.total} 个文件")
        self._statistics(result)
        for batch in plan.batches:
            if self._cancel_requested():
                result.cancelled = True
                break
            if self._finish_requested():
                result.early_stopped = True
                break
            for event in batch.log_events:
                self._log(event.level, event.message)
            for file_plan in batch.files:
                if self._cancel_requested():
                    result.cancelled = True
                    break
                status = self._run_file(file_plan, result)
                if status == "cancelled":
                    result.cancelled = True
                    break
                setattr(result, status, getattr(result, status) + 1)
                if self._on_progress is not None:
                    self._on_progress(TaskProgress(
                        result.ok + result.failed + result.skipped, result.total,
                        file_plan.source_path.name,
                    ))
                self._statistics(result)
            if result.cancelled:
                break
        if result.cancelled:
            self._log(LogLevel.WARN, "已取消，任务中止")
        elif result.early_stopped:
            self._log(LogLevel.WARN, "已按“结束”请求停止：完成当前批次后不再处理后续批次")
        else:
            self._log(LogLevel.INFO, f"任务结束：成功 {result.ok}，失败 {result.failed}，跳过 {result.skipped}")
        self._statistics(result)
        return result
