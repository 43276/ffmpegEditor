"""统一配置入口，保留现有 QSettings 存储位置与设置键。"""
from __future__ import annotations

from PyQt6.QtCore import QSettings


class SettingsService:
    """以 QSettings 兼容接口向页面注入同一个配置对象。"""

    ORGANIZATION = "CompressImages"
    APPLICATION = "ImageConverter"

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings if settings is not None else QSettings(self.ORGANIZATION, self.APPLICATION)

    def value(self, key, defaultValue=None, type=None):
        if type is None:
            return self._settings.value(key, defaultValue)
        return self._settings.value(key, defaultValue, type=type)

    def setValue(self, key, value):  # noqa: N802 -- QSettings-compatible interface
        self._settings.setValue(key, value)

    def sync(self):
        self._settings.sync()

    @property
    def settings(self) -> QSettings:
        return self._settings
