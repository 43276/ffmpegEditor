"""元数据表格负责列、单元格、缩略图和以稳定行 ID 表达的编辑事件。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, LineEdit, TableWidget

from app.audio.formats import METADATA_FIELDS, TEXT_FIELDS
from app.audio.models import TrackEdit
from ui.media_presentation import format_audio_edit

_COVER_THUMB_SMALL = 48
_COVER_THUMB_LARGE = 96
_ROW_HEIGHT_SMALL = 58
_ROW_HEIGHT_LARGE = 106
_COVER_COL_SMALL = 104
_COVER_COL_LARGE = 152


class ClickableLabel(BodyLabel):
    """可点击的 QLabel：封面单元格与格式单元格使用。"""

    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class FileNameLabel(BodyLabel):
    """稳定显示文件名；完整内容由悬停提示提供，避免跑马灯持续重绘。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setMinimumHeight(24)


class CellEditor(QWidget):
    """文本单元格容器：把输入框居中并留边，使其比表格格子小。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = LineEdit(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.addWidget(self.line_edit)


class MetadataTable(TableWidget):
    formatRequested = pyqtSignal(str)
    coverRequested = pyqtSignal(str)
    textEdited = pyqtSignal(str, str, str)
    columnRequested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked_fields = {field.key for field in METADATA_FIELDS if field.default_checked}
        self._cover_zoom = "small"
        self._entries: list[tuple[str, TrackEdit]] = []
        self._rows: list[TrackEdit] = []
        self._indices: dict[str, int] = {}
        self.setFixedHeight(400)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(False)
        self.horizontalHeader().sectionClicked.connect(self._on_header)
        self._RebuildTable()

    def set_entries(self, entries) -> None:
        self._entries = list(entries)
        self._rows = [row for _row_id, row in self._entries]
        self._indices.clear()
        self._RebuildTable()

    def set_fields(self, keys) -> None:
        self._checked_fields = set(keys)
        self.set_entries(self._entries)

    def set_cover_zoom(self, zoom: str) -> None:
        self._cover_zoom = zoom
        self._ApplyCoverZoom()

    def add_row(self, row_id: str, row: TrackEdit) -> None:
        if row_id in self._indices:
            self.update_row(row_id, row)
            return
        self._entries.append((row_id, row))
        self._rows.append(row)
        self._AppendRow(row_id, row)

    def update_row(self, row_id: str, row: TrackEdit) -> None:
        index = self.row_index(row_id)
        if index < 0:
            return
        self._entries[index] = (row_id, row)
        self._rows[index] = row
        self._UpdateRowCells(row_id, row)

    def _on_header(self, section: int) -> None:
        columns = self._Columns()
        if 0 <= section < len(columns):
            _title, kind, key = columns[section]
            if kind != "name":
                self.columnRequested.emit(kind, key)

    def _Columns(self) -> list[tuple[str, str, str]]:
        """当前列定义：[（表头文本, 类型, 字段 key）]，类型为 format/name/cover/text。"""
        columns: list[tuple[str, str, str]] = [
            ("格式", "format", "format"),
            ("文件名", "name", "name"),
        ]
        if "cover" in self._checked_fields:
            columns.append(("封面", "cover", "cover"))
        for item in TEXT_FIELDS:
            if item.key in self._checked_fields:
                columns.append((item.display_name, "text", item.key))
        return columns

    def _ColumnCount(self) -> int:
        return len(self._Columns())

    def _CoverColumn(self) -> int:
        for index, (_text, kind, _key) in enumerate(self._Columns()):
            if kind == "cover":
                return index
        return -1

    def _ColumnIndexOfField(self, key: str) -> int:
        for index, (_text, _kind, column_key) in enumerate(self._Columns()):
            if column_key == key:
                return index
        return -1

    def _FieldKeyForColumn(self, section: int) -> str | None:
        columns = self._Columns()
        if 0 <= section < len(columns):
            _text, kind, key = columns[section]
            if kind == "text":
                return key
        return None

    def row_index(self, row_id: str) -> int:
        return self._indices.get(row_id, -1)

    def _RebuildTable(self) -> None:
        self.clearContents()
        self.setRowCount(0)

        headers = [text for text, _kind, _key in self._Columns()]

        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)

        header = self.horizontalHeader()
        for col, (_text, kind, _key) in enumerate(self._Columns()):
            if kind in ("format", "cover"):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, 128)

        for row_id, row in self._entries:
            self._AppendRow(row_id, row)
        self._ApplyCoverZoom()

    def _AppendRow(self, row_id: str, row: TrackEdit) -> None:
        row_index = self.rowCount()
        self._indices[row_id] = row_index
        self.insertRow(row_index)
        self._FillRow(row_index, row_id, row)
        self._ApplyCoverZoomForRow(row_index, row)

    def _FillRow(self, row_index: int, row_id: str, row: TrackEdit) -> None:
        format_cell = ClickableLabel(format_audio_edit(row))
        format_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
        format_cell.setToolTip("点击修改该文件的格式")
        format_cell.clicked.connect(lambda _checked=False, row_id=row_id: self.formatRequested.emit(row_id))
        self.setCellWidget(row_index, 0, format_cell)

        name_cell = FileNameLabel(row.file_name)
        name_cell.setToolTip(row.file_name)
        self.setCellWidget(row_index, 1, name_cell)

        col = 2
        if "cover" in self._checked_fields:
            cover_cell = ClickableLabel()
            cover_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cover_cell.setToolTip("点击修改该文件的封面")
            cover_cell.clicked.connect(lambda _checked=False, row_id=row_id: self.coverRequested.emit(row_id))
            self.setCellWidget(row_index, col, cover_cell)
            self._UpdateCoverCell(row, cover_cell)
            col += 1

        for item in TEXT_FIELDS:
            if item.key not in self._checked_fields:
                continue
            container = CellEditor(self)
            editor = container.line_edit
            editor.setText(row.edited_values.get(item.key, ""))
            editor.setPlaceholderText(row.original_values.get(item.key, ""))
            editor.setToolTip(item.display_name)
            editor.textEdited.connect(
                lambda value, row_id=row_id, key=item.key: self.textEdited.emit(row_id, key, value)
            )
            self.setCellWidget(row_index, col, container)
            col += 1

    def _UpdateRowCells(self, row_id: str, row: TrackEdit) -> None:
        row_index = self.row_index(row_id)
        if row_index < 0 or row_index >= self.rowCount():
            return
        self._FillRow(row_index, row_id, row)
        self._ApplyCoverZoomForRow(row_index, row)

    def _UpdateCoverCell(self, row: TrackEdit, cell: ClickableLabel) -> None:
        cell.setPixmap(QPixmap())
        cell.setText("")
        pixmap: QPixmap | None = None
        if row.cover_action == "remove":
            cell.setText("将移除")
            return
        if row.cover_action == "set" and row.cover_source is not None:
            pixmap = QPixmap(str(row.cover_source))
        elif row.thumbnail_bytes:
            loaded = QPixmap()
            loaded.loadFromData(row.thumbnail_bytes)
            if not loaded.isNull():
                pixmap = loaded
        if pixmap is not None and not pixmap.isNull():
            size = self._CoverThumbSize()
            cell.setPixmap(
                pixmap.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            cell.setText("无")

    def _CoverThumbSize(self) -> int:
        return _COVER_THUMB_LARGE if self._cover_zoom == "large" else _COVER_THUMB_SMALL

    def _ApplyCoverZoom(self) -> None:
        small = self._cover_zoom == "small"
        row_height = _ROW_HEIGHT_SMALL if small else _ROW_HEIGHT_LARGE
        cover_width = _COVER_COL_SMALL if small else _COVER_COL_LARGE
        self.verticalHeader().setDefaultSectionSize(row_height)
        for row_index in range(self.rowCount()):
            self.setRowHeight(row_index, row_height)
        cover_col = self._CoverColumn()
        if cover_col >= 0:
            self.setColumnWidth(cover_col, cover_width)
            for row_index in range(self.rowCount()):
                cell = self.cellWidget(row_index, cover_col)
                if isinstance(cell, ClickableLabel) and row_index < len(self._rows):
                    self._UpdateCoverCell(self._rows[row_index], cell)

    def _ApplyCoverZoomForRow(self, row_index: int, row: TrackEdit) -> None:
        row_height = _ROW_HEIGHT_SMALL if self._cover_zoom == "small" else _ROW_HEIGHT_LARGE
        self.setRowHeight(row_index, row_height)
        cover_col = self._CoverColumn()
        if cover_col >= 0:
            cell = self.cellWidget(row_index, cover_col)
            if isinstance(cell, ClickableLabel):
                self._UpdateCoverCell(row, cell)
