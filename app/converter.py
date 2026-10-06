"""执行完整命令：进程启动、取消、超时及输出收集。"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from contextlib import ExitStack
from typing import Callable, Sequence

from .task_models import ProcessResult


def execute(
    cmd: Sequence[str],
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 900.0,
    *, capture_stdout: bool = False,
) -> ProcessResult:
    """执行命令并返回结构化结果；输出用临时文件收集，避免管道阻塞。"""
    if cancel_check is not None and cancel_check():
        return ProcessResult(-1, cancelled=True)
    cancelled = False
    timed_out = False
    with ExitStack() as stack:
        error_file = stack.enter_context(tempfile.TemporaryFile())
        output_file = stack.enter_context(tempfile.TemporaryFile()) if capture_stdout else None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=output_file if output_file is not None else subprocess.DEVNULL,
                stderr=error_file,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            return ProcessResult(-1, f"无法启动命令：{exc}")
        try:
            started_at = time.monotonic()
            while proc.poll() is None:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                if time.monotonic() - started_at > timeout_seconds:
                    timed_out = True
                    break
                time.sleep(0.05)
        finally:
            # 包括取消回调抛异常的情况，均不遗留正在写临时文件的子进程。
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        error_file.seek(0, os.SEEK_END)
        error_file.seek(max(0, error_file.tell() - 6000))
        stderr = error_file.read().decode("utf-8", errors="replace").strip()[-1500:]
        if timed_out:
            stderr = "处理超时，已强制终止" + (f"\n{stderr}" if stderr else "")
        stdout = b""
        if output_file is not None:
            output_file.seek(0)
            stdout = output_file.read()
        return ProcessResult(proc.returncode, stderr, cancelled, timed_out, stdout)
