"""音频字段、格式与容器能力规则。"""
from __future__ import annotations

from .models import AudioFormatOption, MetadataField


AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".alac",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
}


# ---- 字段模型 ---------------------------------------------------------

METADATA_FIELDS: tuple[MetadataField, ...] = (
    MetadataField("cover", "封面", "cover", True),
    MetadataField("title", "标题", "text", True),
    MetadataField("artist", "作者", "artist", True),
    MetadataField("album", "专辑", "text", True),
    MetadataField("album_artist", "专辑艺术家", "text", False),
    MetadataField("date", "年份", "text", False),
    MetadataField("track", "音轨号", "text", False),
    MetadataField("genre", "流派", "text", False),
    MetadataField("comment", "注释", "text", False),
)

TEXT_FIELDS: tuple[MetadataField, ...] = tuple(
    item for item in METADATA_FIELDS if item.kind != "cover"
)

_FIELDS_BY_KEY = {item.key: item for item in METADATA_FIELDS}


def field_by_key(key: str) -> MetadataField | None:
    return _FIELDS_BY_KEY.get(key)


# ---- 格式转换模型 -----------------------------------------------------

FORMAT_OPTIONS: tuple[AudioFormatOption, ...] = (
    AudioFormatOption("keep", "保持原格式", None, False, (), False),
    AudioFormatOption("mp3", "MP3", ".mp3", True, ("128k", "192k", "256k", "320k"), True),
    AudioFormatOption("m4a", "M4A (AAC)", ".m4a", True, ("128k", "192k", "256k", "320k"), True),
    AudioFormatOption("flac", "FLAC", ".flac", False, (), True),
    AudioFormatOption("ogg", "OGG (Vorbis)", ".ogg", True, ("96k", "128k", "192k", "256k"), False),
    AudioFormatOption("opus", "OPUS", ".opus", True, ("64k", "96k", "128k", "192k"), False),
    AudioFormatOption("wav", "WAV (PCM)", ".wav", False, (), False),
    AudioFormatOption("aac", "AAC (ADTS)", ".aac", True, ("128k", "192k", "256k", "320k"), False),
)

_FORMATS_BY_KEY = {item.key: item for item in FORMAT_OPTIONS}
DEFAULT_BITRATE = "192k"
CONVERT_FORMATS: tuple[AudioFormatOption, ...] = tuple(
    item for item in FORMAT_OPTIONS if item.key != "keep"
)
WAV_AUTO_CONVERT_TARGETS: tuple[AudioFormatOption, ...] = tuple(
    item for item in FORMAT_OPTIONS if item.cover_capable
)

# 容器不支持内嵌封面的目标扩展名：写入时自动忽略封面修改并给出警告
COVER_UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".aac", ".ogg", ".opus"}
)

# 容器不保存元数据标签的目标扩展名：元数据修改会被静默丢弃，需显式警告
TAG_UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".aac"})


def format_by_key(key: str | None) -> AudioFormatOption | None:
    if key is None:
        return None
    return _FORMATS_BY_KEY.get(key)
