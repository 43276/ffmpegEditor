"""用户要求的桌面操作；保留 Windows 隐藏控制台与关机条件。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.task_models import TaskResult


class DesktopActions:
    def open_output_directory(self, directory: str | Path) -> bool:
        if sys.platform != "win32" or not Path(directory).is_dir():
            return False
        os.startfile(str(directory))  # noqa: S606 -- explicit user action
        return True

    def schedule_shutdown(self, result: TaskResult, requested: bool) -> bool:
        if not requested or result.cancelled or result.early_stopped or sys.platform != "win32":
            return False
        try:
            completed = subprocess.run(
                ["shutdown", "/s", "/t", "60"], check=False, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("关机命令执行超时") from exc
        if completed.returncode:
            raise OSError(f"关机命令返回错误码：{completed.returncode}")
        return True
