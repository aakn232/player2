from __future__ import annotations

import argparse
import bisect
import locale
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def _bootstrap_mpv_dll_path() -> None:
    if os.name != "nt":
        return

    candidates = [
        Path(__file__).resolve().parent / "mpv",
        Path.cwd() / "mpv",
    ]

    for folder in candidates:
        dll = folder / "libmpv-2.dll"
        if not dll.exists():
            continue

        os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(folder))
        except (AttributeError, OSError):
            pass
        return


_bootstrap_mpv_dll_path()

try:
    import mpv  # type: ignore[import-not-found]
except Exception as exc:  # pragma: no cover - runtime environment dependent
    mpv = None
    MPV_IMPORT_ERROR = str(exc)
else:
    MPV_IMPORT_ERROR = ""

DEFAULT_SEGMENT_MS = 60_000
FILE_TIME_RE = re.compile(r"(?P<mm>\d{2})M(?P<ss>\d{2})S(?:_(?P<ts>\d+))?", re.IGNORECASE)
FOLDER_TIME_RE = re.compile(r"^\d{10}$")
SETTINGS_ORG = "homecam-player"
SETTINGS_APP = "timeline-player"
SETTINGS_LAST_FOLDER = "last_open_folder"
SETTINGS_VOLUME = "volume"
GAP_THRESHOLD_MS = 10_000
SCRUB_PREVIEW_INTERVAL_MS = 33


@dataclass
class Segment:
    path: Optional[Path]
    source_dt: Optional[datetime]
    duration_ms: int = DEFAULT_SEGMENT_MS
    start_ms: int = 0
    kind: str = "media"
    issue_reason: str = ""


@dataclass
class ValidationIssue:
    path: Path
    reason: str


class SegmentLoaderWorker(QObject):
    progress = Signal(int, int, int)
    finished = Signal(object, object, str)
    failed = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    @Slot()
    def run(self) -> None:
        try:
            segments, invalid_files, validation_mode = build_segment_index(
                self.root,
                progress_callback=self._emit_progress,
            )
            self.finished.emit(segments, invalid_files, validation_mode)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _emit_progress(self, current: int, total: int) -> None:
        if total <= 0:
            percent = 100
        else:
            percent = int((current / total) * 100)
        self.progress.emit(current, total, percent)


class ClickableSlider(QSlider):
    def __init__(self, orientation: Qt.Orientation, parent: Optional[QWidget] = None) -> None:
        super().__init__(orientation, parent)
        self.highlight_ranges: list[tuple[int, int, QColor]] = []

    def set_highlight_ranges(self, ranges: list[tuple[int, int, QColor]]) -> None:
        self.highlight_ranges = ranges
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            value = QStyle.sliderValueFromPosition(
                self.minimum(),
                self.maximum(),
                int(event.position().x()),
                self.width(),
            )
            self.setSliderPosition(value)
            self.sliderMoved.emit(value)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if not self.highlight_ranges:
            return
        if self.maximum() <= self.minimum():
            return

        span = self.maximum() - self.minimum()
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        if not groove.isValid():
            return

        groove = groove.adjusted(1, 1, -1, -1)
        if groove.width() <= 1 or groove.height() <= 1:
            return

        painter = QPainter(self)
        painter.setPen(Qt.NoPen)
        painter.setRenderHint(QPainter.Antialiasing, True)
        radius = max(1.0, min(3.0, groove.height() / 2.0))

        for start, end, color in self.highlight_ranges:
            norm_start = max(self.minimum(), min(start, self.maximum()))
            norm_end = max(self.minimum(), min(end, self.maximum()))
            if norm_end <= norm_start:
                continue

            x1 = groove.left() + int(((norm_start - self.minimum()) / span) * max(1, groove.width() - 1))
            x2 = groove.left() + int(((norm_end - self.minimum()) / span) * max(1, groove.width() - 1))
            if x2 <= x1:
                x2 = x1 + 1

            fill = QColor(color)
            fill.setAlpha(min(130, color.alpha()))
            painter.setBrush(fill)
            painter.drawRoundedRect(x1, groove.top(), x2 - x1, groove.height(), radius, radius)

        painter.end()


def parse_source_datetime(path: Path) -> Optional[datetime]:
    match = FILE_TIME_RE.search(path.stem)
    if not match:
        return None

    minute = int(match.group("mm"))
    second = int(match.group("ss"))
    ts = match.group("ts")

    if ts:
        try:
            return datetime.fromtimestamp(int(ts))
        except (OverflowError, ValueError):
            pass

    folder = path.parent.name
    if FOLDER_TIME_RE.match(folder):
        try:
            base_dt = datetime.strptime(folder, "%Y%m%d%H")
            return base_dt + timedelta(minutes=minute, seconds=second)
        except ValueError:
            return None

    return None


def _validate_mp4_with_moov_atom(file_path: Path) -> tuple[bool, str]:
    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        return False, f"Read error: {exc}"

    if file_size < 8:
        return False, "File too small"

    try:
        with file_path.open("rb") as f:
            cursor = 0
            has_moov = False

            while cursor + 8 <= file_size:
                header = f.read(8)
                if len(header) < 8:
                    break

                atom_size = int.from_bytes(header[0:4], "big")
                atom_type = header[4:8]
                header_size = 8

                if atom_size == 1:
                    extended = f.read(8)
                    if len(extended) < 8:
                        return False, "Invalid extended atom size"
                    atom_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - cursor

                if atom_size < header_size:
                    return False, "Invalid atom size"

                if atom_type == b"moov":
                    has_moov = True
                    break

                skip = atom_size - header_size
                if skip > 0:
                    f.seek(skip, 1)

                cursor += atom_size

            if not has_moov:
                return False, "moov atom not found"

            return True, "ok"
    except OSError as exc:
        return False, f"Read error: {exc}"


def build_segment_index(
    root: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[Segment], list[ValidationIssue], str]:
    files = sorted(root.rglob("*.mp4"))
    total_files = len(files)
    validation_mode = "moov-atom"
    invalid_files: list[ValidationIssue] = []

    probed: list[Segment] = []
    for index, file_path in enumerate(files, start=1):
        parsed_dt = parse_source_datetime(file_path)
        is_valid, reason = _validate_mp4_with_moov_atom(file_path)
        duration_ms = DEFAULT_SEGMENT_MS

        if is_valid:
            probed.append(
                Segment(
                    path=file_path,
                    source_dt=parsed_dt,
                    duration_ms=duration_ms,
                    kind="media",
                )
            )
        else:
            invalid_files.append(ValidationIssue(path=file_path, reason=reason))
            probed.append(
                Segment(
                    path=file_path,
                    source_dt=parsed_dt,
                    duration_ms=duration_ms,
                    kind="invalid",
                    issue_reason=reason,
                )
            )

        if progress_callback is not None:
            progress_callback(index, total_files)

    def sort_key(seg: Segment):
        name = str(seg.path).lower() if seg.path else ""
        if seg.source_dt is not None:
            return (0, seg.source_dt, name)
        return (1, datetime.min, name)

    probed.sort(key=sort_key)

    timeline: list[Segment] = []
    previous_end: Optional[datetime] = None
    for seg in probed:
        if previous_end is not None and seg.source_dt is not None:
            gap_ms = int((seg.source_dt - previous_end).total_seconds() * 1000)
            if gap_ms > GAP_THRESHOLD_MS:
                timeline.append(
                    Segment(
                        path=None,
                        source_dt=previous_end,
                        duration_ms=gap_ms,
                        kind="gap",
                        issue_reason="Missing recording interval",
                    )
                )

        timeline.append(seg)

        if seg.source_dt is not None:
            previous_end = seg.source_dt + timedelta(milliseconds=max(1_000, seg.duration_ms))
        else:
            previous_end = None

    return timeline, invalid_files, validation_mode


def format_hms(ms: int) -> str:
    total_s = max(0, ms) // 1000
    hour, rem = divmod(total_s, 3600)
    minute, second = divmod(rem, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


class HomecamPlayerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Homecam Timeline Player")
        self.resize(1200, 760)

        self.segments: list[Segment] = []
        self.segment_starts: list[int] = []
        self.total_duration_ms = 0
        self.current_index = -1
        self.pending_seek_ms: Optional[int] = None
        self.pending_should_play = False
        self.pending_seek_exact = True
        self.validation_issues: list[ValidationIssue] = []
        self.validation_mode = "unknown"
        self.loaded_root: Optional[Path] = None
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.playback_rate = 1.0
        self.volume_percent = self._load_saved_volume()
        self.unplayable_ranges: list[tuple[int, int, QColor]] = []
        self.virtual_position_ms: Optional[int] = None
        self.is_scrubbing = False
        self.scrub_was_playing = False
        self.pending_scrub_value: Optional[int] = None
        self.is_loading = False
        self.loader_thread: Optional[QThread] = None
        self.loader_worker: Optional[SegmentLoaderWorker] = None
        self.mpv_ready = False
        self.last_end_handled_index = -1

        self._setup_ui()
        self._setup_player()
        self._setup_scrub_preview_timer()
        self._setup_timer()

    def _setup_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        self.video_widget = QWidget()
        self.video_widget.setAttribute(Qt.WA_NativeWindow, True)
        self.video_widget.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        layout.addWidget(self.video_widget, 1)

        self.timeline_slider = ClickableSlider(Qt.Horizontal)
        self.timeline_slider.setRange(0, 0)
        self.timeline_slider.sliderPressed.connect(self._on_slider_pressed)
        self.timeline_slider.sliderReleased.connect(self._on_slider_released)
        self.timeline_slider.sliderMoved.connect(self._on_slider_preview)
        layout.addWidget(self.timeline_slider)

        control_row = QHBoxLayout()
        layout.addLayout(control_row)

        self.open_button = QPushButton("Open Folder")
        self.open_button.clicked.connect(self._open_folder_dialog)
        control_row.addWidget(self.open_button)

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle_play)
        control_row.addWidget(self.play_button)

        speed_label = QLabel("Speed")
        control_row.addWidget(speed_label)
        self.speed_group = QButtonGroup(self)
        self.speed_group.setExclusive(True)
        self.speed_buttons: dict[float, QPushButton] = {}
        for rate in (1.0, 4.0, 8.0, 16.0):
            button = QPushButton(f"{int(rate)}x")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked, r=rate: self._set_speed(r))
            self.speed_group.addButton(button)
            self.speed_buttons[rate] = button
            control_row.addWidget(button)
        self._set_speed(1.0)

        volume_label = QLabel("Volume")
        control_row.addWidget(volume_label)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(110)
        self.volume_slider.setValue(self.volume_percent)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        control_row.addWidget(self.volume_slider)
        self.volume_value_label = QLabel(f"{self.volume_percent}%")
        control_row.addWidget(self.volume_value_label)

        self.invalid_files_button = QPushButton("Invalid Files (0)")
        self.invalid_files_button.setEnabled(False)
        self.invalid_files_button.clicked.connect(self._show_invalid_files)
        control_row.addWidget(self.invalid_files_button)

        self.time_label = QLabel("00:00:00 / 00:00:00")
        control_row.addWidget(self.time_label, 1)

        self.folder_label = QLabel("No folder loaded")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)

        self.loading_label = QLabel("")
        self.loading_label.setWordWrap(True)
        layout.addWidget(self.loading_label)

        self.segment_label = QLabel("")
        self.segment_label.setWordWrap(True)
        layout.addWidget(self.segment_label)

        open_action = QAction("Open Folder", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open_folder_dialog)
        self.addAction(open_action)

    def _setup_player(self) -> None:
        if mpv is None:
            raise RuntimeError(
                "python-mpv import failed. Install python-mpv and ensure libmpv-2.dll is in mpv folder. "
                f"detail: {MPV_IMPORT_ERROR}"
            )

        # libmpv requires C numeric locale in GUI apps.
        locale.setlocale(locale.LC_NUMERIC, "C")

        self.mpv_player = mpv.MPV(
            wid=str(int(self.video_widget.winId())),
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            keep_open="always",
        )
        self.mpv_player.pause = True
        self.mpv_player.speed = self.playback_rate
        self.mpv_player.volume = self.volume_percent
        self.mpv_ready = True

    def _setup_timer(self) -> None:
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(120)
        self.ui_timer.timeout.connect(self._sync_timeline)
        self.ui_timer.start()

    def _setup_scrub_preview_timer(self) -> None:
        self.scrub_preview_timer = QTimer(self)
        self.scrub_preview_timer.setSingleShot(True)
        self.scrub_preview_timer.setInterval(SCRUB_PREVIEW_INTERVAL_MS)
        self.scrub_preview_timer.timeout.connect(self._run_scrub_preview)

    def _open_folder_dialog(self) -> None:
        if self.is_loading:
            return
        start_dir = self._suggest_open_folder()
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select root folder with mp4 files",
            start_dir,
        )
        if not selected:
            return
        self.load_folder(Path(selected))

    def load_folder(self, root: Path) -> None:
        if self.is_loading:
            QMessageBox.information(self, "Loading", "이미 폴더를 로딩 중입니다. 완료 후 다시 시도하세요.")
            return

        self.loaded_root = root
        self.settings.setValue(SETTINGS_LAST_FOLDER, str(root))
        self.validation_issues = []
        self._update_invalid_files_button()
        self._pause_playback()
        self.segment_label.setText("")
        self._set_loading_state(True)

        self.loader_thread = QThread(self)
        self.loader_worker = SegmentLoaderWorker(root)
        self.loader_worker.moveToThread(self.loader_thread)

        self.loader_thread.started.connect(self.loader_worker.run)
        self.loader_worker.progress.connect(self._on_load_progress)
        self.loader_worker.finished.connect(self._on_load_finished)
        self.loader_worker.failed.connect(self._on_load_failed)

        self.loader_worker.finished.connect(self.loader_thread.quit)
        self.loader_worker.failed.connect(self.loader_thread.quit)
        self.loader_thread.finished.connect(self.loader_worker.deleteLater)
        self.loader_thread.finished.connect(self.loader_thread.deleteLater)
        self.loader_thread.finished.connect(self._on_loader_thread_finished)

        self.loader_thread.start()

    def _set_loading_state(self, loading: bool) -> None:
        self.is_loading = loading
        self.open_button.setEnabled(not loading)
        self.play_button.setEnabled(not loading)
        self.timeline_slider.setEnabled(not loading)
        if loading:
            self.loading_label.setText("Loading... total: 0 | current: 0 | 0%")
            self.invalid_files_button.setEnabled(False)
        else:
            self.invalid_files_button.setEnabled(len(self.validation_issues) > 0)

    @Slot(int, int, int)
    def _on_load_progress(self, current: int, total: int, percent: int) -> None:
        self.loading_label.setText(f"Loading... total: {total} | current: {current} | {percent}%")

    @Slot(object, object, str)
    def _on_load_finished(self, segments_obj: object, invalid_obj: object, validation_mode: str) -> None:
        segments = list(segments_obj)
        invalid_files = list(invalid_obj)

        self.validation_issues = invalid_files
        self.validation_mode = validation_mode
        self._update_invalid_files_button()

        total_mp4 = sum(1 for seg in segments if seg.kind != "gap")
        if total_mp4 == 0:
            self.segments = []
            self.segment_starts.clear()
            self.total_duration_ms = 0
            self.timeline_slider.setRange(0, 0)
            self.timeline_slider.setValue(0)
            self.timeline_slider.set_highlight_ranges([])
            self._update_time_label(0)
            self.loading_label.setText("Load complete. total: 0 | current: 0 | 100%")
            QMessageBox.warning(self, "No MP4", "No MP4 files were found in the selected folder.")
            return

        self.segments = segments
        self._rebuild_timeline()
        playable_count = sum(1 for seg in self.segments if seg.kind == "media")
        gap_count = sum(1 for seg in self.segments if seg.kind == "gap")
        self.segment_label.setText("")
        self.folder_label.setText(
            f"Loaded: {self.loaded_root} | playable: {playable_count} | invalid: {len(invalid_files)} | gaps: {gap_count} | validation: {validation_mode}"
        )
        self.loading_label.setText(
            f"Load complete. total: {total_mp4} | current: {total_mp4} | 100%"
        )

        first_playable = self._find_next_playable_index(-1)
        if first_playable is None:
            self.current_index = -1
            self.virtual_position_ms = 0
            self._pause_playback()
            self.segment_label.setText("No playable files. Check Invalid Files and timeline marks.")
            return

        self._load_segment(first_playable, seek_ms=0, should_play=True)

    @Slot(str)
    def _on_load_failed(self, message: str) -> None:
        self.loading_label.setText("Load failed.")
        QMessageBox.warning(self, "Load failed", f"영상 로딩 중 오류가 발생했습니다.\n{message}")

    @Slot()
    def _on_loader_thread_finished(self) -> None:
        self.loader_worker = None
        self.loader_thread = None
        self._set_loading_state(False)

    def _update_invalid_files_button(self) -> None:
        count = len(self.validation_issues)
        self.invalid_files_button.setText(f"Invalid Files ({count})")
        self.invalid_files_button.setEnabled(count > 0)

    def _show_invalid_files(self) -> None:
        if not self.validation_issues:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Invalid MP4 Files")
        dialog.resize(900, 520)

        layout = QVBoxLayout(dialog)
        summary = QLabel(
            f"Validation mode: {self.validation_mode} | invalid files: {len(self.validation_issues)}"
        )
        layout.addWidget(summary)

        text = QTextEdit()
        text.setReadOnly(True)
        detail_lines = [
            f"- {self._issue_display_path(issue.path)}: {issue.reason}" for issue in self.validation_issues
        ]
        text.setPlainText("\n".join(detail_lines))
        layout.addWidget(text)

        button_row = QHBoxLayout()
        layout.addLayout(button_row)

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        button_row.addWidget(close_button)

        dialog.exec()

    def _issue_display_path(self, file_path: Path) -> str:
        if self.loaded_root is not None:
            try:
                return str(file_path.relative_to(self.loaded_root))
            except ValueError:
                pass
        return file_path.name

    def _rebuild_timeline(self) -> None:
        self.segment_starts.clear()
        self.unplayable_ranges.clear()
        cursor = 0
        for seg in self.segments:
            seg.start_ms = cursor
            self.segment_starts.append(cursor)
            seg_len = max(1_000, int(seg.duration_ms))
            if seg.kind == "gap":
                self.unplayable_ranges.append((cursor, cursor + seg_len, QColor(255, 193, 7, 180)))
            elif seg.kind == "invalid":
                self.unplayable_ranges.append((cursor, cursor + seg_len, QColor(220, 53, 69, 180)))
            cursor += seg_len

        self.total_duration_ms = cursor
        max_value = max(0, self.total_duration_ms - 1)
        self.timeline_slider.setRange(0, max_value)
        self.timeline_slider.set_highlight_ranges(self.unplayable_ranges)
        self._update_time_label(0)

    def _update_time_label(self, global_ms: int) -> None:
        self.time_label.setText(f"{format_hms(global_ms)} / {format_hms(self.total_duration_ms)}")

    def _map_global_to_segment(self, global_ms: int) -> tuple[int, int]:
        if not self.segments:
            return -1, 0

        target = max(0, min(global_ms, max(0, self.total_duration_ms - 1)))
        idx = bisect.bisect_right(self.segment_starts, target) - 1
        idx = max(0, min(idx, len(self.segments) - 1))
        offset = target - self.segments[idx].start_ms
        return idx, max(0, offset)

    def _current_global_position(self) -> int:
        if self.virtual_position_ms is not None:
            return self.virtual_position_ms
        if self.current_index < 0 or self.current_index >= len(self.segments):
            return 0
        if self.segments[self.current_index].kind != "media":
            return self.segments[self.current_index].start_ms
        return self.segments[self.current_index].start_ms + max(0, self._current_file_position_ms())

    def _load_segment(self, index: int, seek_ms: int, should_play: bool, seek_exact: bool = True) -> int:
        if index < 0 or index >= len(self.segments):
            return 0

        segment = self.segments[index]
        if segment.kind != "media" or segment.path is None:
            self.current_index = index
            self.virtual_position_ms = segment.start_ms + max(0, min(seek_ms, segment.duration_ms - 1))
            self._pause_playback()
            if segment.kind == "gap":
                self.segment_label.setText("No recording in this time interval (timeline gap).")
            else:
                self.segment_label.setText("")
            return self.virtual_position_ms

        self.current_index = index
        self.virtual_position_ms = None
        self.pending_seek_ms = max(0, seek_ms)
        self.pending_should_play = should_play
        self.pending_seek_exact = seek_exact

        self.segment_label.setText("")
        if not self.mpv_ready:
            return segment.start_ms + self.pending_seek_ms

        self.mpv_player.pause = True
        self.mpv_player.speed = self.playback_rate
        self.mpv_player.volume = self.volume_percent
        self.mpv_player.command("loadfile", str(segment.path), "replace")
        QTimer.singleShot(0, self._finalize_pending_seek)
        return segment.start_ms + self.pending_seek_ms

    def _find_previous_playable_index(self, index: int) -> Optional[int]:
        for prev_index in range(index - 1, -1, -1):
            seg = self.segments[prev_index]
            if seg.kind == "media" and seg.path is not None:
                return prev_index
        return None

    def _snap_global_to_playable(self, global_ms: int) -> int:
        if not self.segments:
            return 0

        idx, offset = self._map_global_to_segment(global_ms)
        target_segment = self.segments[idx]
        if target_segment.kind == "media" and target_segment.path is not None:
            return target_segment.start_ms + offset

        next_playable = self._find_next_playable_index(idx)
        if next_playable is not None:
            return self.segments[next_playable].start_ms

        prev_playable = self._find_previous_playable_index(idx)
        if prev_playable is not None:
            prev_seg = self.segments[prev_playable]
            return prev_seg.start_ms + max(0, prev_seg.duration_ms - 1)

        return target_segment.start_ms

    def _seek_global(
        self,
        global_ms: int,
        should_play_override: Optional[bool] = None,
        seek_exact: bool = True,
    ) -> int:
        if not self.segments:
            return 0

        snapped_global = self._snap_global_to_playable(global_ms)
        idx, offset = self._map_global_to_segment(snapped_global)
        if should_play_override is None:
            should_play = self._is_playing()
        else:
            should_play = should_play_override

        target_segment = self.segments[idx]
        if target_segment.kind != "media" or target_segment.path is None:
            self._pause_playback()
            self.virtual_position_ms = target_segment.start_ms
            return self.virtual_position_ms

        if idx == self.current_index:
            segment = self.segments[idx]
            if segment.kind != "media":
                self.virtual_position_ms = segment.start_ms + offset
                self._update_time_label(self.virtual_position_ms)
                return self.virtual_position_ms
            self._seek_current_file(offset, exact=seek_exact)
            if should_play:
                self._resume_playback()
            return segment.start_ms + offset

        return self._load_segment(idx, seek_ms=offset, should_play=should_play, seek_exact=seek_exact)

    def _on_slider_pressed(self) -> None:
        if not self.segments:
            return
        self.is_scrubbing = True
        self.pending_scrub_value = None
        self.scrub_was_playing = self._is_playing()
        if self.scrub_was_playing:
            self._pause_playback()

    def _on_slider_preview(self, value: int) -> None:
        if not self.segments:
            return

        snapped = self._snap_global_to_playable(value)
        self.pending_scrub_value = snapped
        self._update_time_label(snapped)

        if not self.scrub_preview_timer.isActive():
            self.scrub_preview_timer.start()

    def _run_scrub_preview(self) -> None:
        if not self.is_scrubbing or self.pending_scrub_value is None:
            return

        snapped = self.pending_scrub_value
        self.pending_scrub_value = None

        self.timeline_slider.blockSignals(True)
        self.timeline_slider.setValue(max(0, min(snapped, self.timeline_slider.maximum())))
        self.timeline_slider.blockSignals(False)

        self._seek_global(snapped, should_play_override=False, seek_exact=False)
        self._update_time_label(snapped)

        if self.pending_scrub_value is not None:
            self.scrub_preview_timer.start()

    def _on_slider_released(self) -> None:
        if self.scrub_preview_timer.isActive():
            self.scrub_preview_timer.stop()
        self.pending_scrub_value = None

        should_resume = self.scrub_was_playing
        self.is_scrubbing = False
        self.scrub_was_playing = False

        snapped = self._seek_global(
            self.timeline_slider.value(),
            should_play_override=should_resume,
            seek_exact=True,
        )
        self.timeline_slider.blockSignals(True)
        self.timeline_slider.setValue(max(0, min(snapped, self.timeline_slider.maximum())))
        self.timeline_slider.blockSignals(False)
        self._update_time_label(snapped)

    def _toggle_play(self) -> None:
        if not self.segments:
            QMessageBox.information(self, "No media", "Load a folder first.")
            return

        if self._is_playing():
            self._pause_playback()
        else:
            if self.current_index < 0:
                first_playable = self._find_next_playable_index(-1)
                if first_playable is not None:
                    self._load_segment(first_playable, seek_ms=0, should_play=True)
            else:
                segment = self.segments[self.current_index]
                if segment.kind == "media":
                    self._resume_playback()
                else:
                    next_playable = self._find_next_playable_index(self.current_index)
                    if next_playable is not None:
                        self._load_segment(next_playable, seek_ms=0, should_play=True)

    def _suggest_open_folder(self) -> str:
        saved = self.settings.value(SETTINGS_LAST_FOLDER, "", str)
        if saved and Path(saved).exists():
            return saved
        if self.loaded_root and self.loaded_root.exists():
            return str(self.loaded_root)
        return str(Path.cwd())

    def _load_saved_volume(self) -> int:
        raw = self.settings.value(SETTINGS_VOLUME, 100)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 100
        return max(0, min(100, value))

    def _set_speed(self, rate: float) -> None:
        self.playback_rate = rate
        for speed, button in self.speed_buttons.items():
            button.setChecked(speed == rate)
        if hasattr(self, "mpv_player"):
            self.mpv_player.speed = self.playback_rate

    def _on_volume_changed(self, value: int) -> None:
        self.volume_percent = max(0, min(100, value))
        if hasattr(self, "mpv_player"):
            self.mpv_player.volume = self.volume_percent
        self.volume_value_label.setText(f"{self.volume_percent}%")
        self.settings.setValue(SETTINGS_VOLUME, self.volume_percent)

    def _is_playing(self) -> bool:
        if not self.mpv_ready:
            return False
        try:
            return not bool(self.mpv_player.pause)
        except Exception:
            return False

    def _pause_playback(self) -> None:
        if self.mpv_ready:
            self.mpv_player.pause = True

    def _resume_playback(self) -> None:
        if self.mpv_ready:
            self.mpv_player.pause = False

    def _seek_current_file(self, ms: int, exact: bool = True) -> None:
        if not self.mpv_ready:
            return
        try:
            mode = "exact" if exact else "keyframes"
            self.mpv_player.command("seek", max(0.0, ms / 1000.0), "absolute", mode)
        except Exception:
            pass

    def _current_file_position_ms(self) -> int:
        if not self.mpv_ready:
            return 0
        try:
            value = self.mpv_player.time_pos
            if value is None:
                return 0
            return max(0, int(float(value) * 1000))
        except Exception:
            return 0

    def _current_file_duration_ms(self) -> int:
        if not self.mpv_ready:
            return 0
        try:
            value = self.mpv_player.duration
            if value is None:
                return 0
            return max(0, int(float(value) * 1000))
        except Exception:
            return 0

    def _is_target_file_loaded(self, target_path: Path) -> bool:
        if not self.mpv_ready:
            return False
        try:
            current_path = self.mpv_player.path
            if not current_path:
                return False
            return Path(str(current_path)).resolve() == target_path.resolve()
        except Exception:
            return False

    def _finalize_pending_seek(self) -> None:
        if self.pending_seek_ms is None:
            return
        if not (0 <= self.current_index < len(self.segments)):
            return
        segment = self.segments[self.current_index]
        if segment.kind != "media":
            self.pending_seek_ms = None
            return
        if segment.path is None:
            self.pending_seek_ms = None
            return
        if not self._is_target_file_loaded(segment.path):
            return

        target_seek = self.pending_seek_ms
        should_play = self.pending_should_play
        seek_exact = self.pending_seek_exact
        self.pending_seek_ms = None

        self._seek_current_file(target_seek, exact=seek_exact)
        if should_play:
            self._resume_playback()
        else:
            self._pause_playback()

    def _update_current_segment_duration(self) -> None:
        if self.current_index < 0 or self.current_index >= len(self.segments):
            return

        segment = self.segments[self.current_index]
        if segment.kind != "media":
            return

        duration_ms = self._current_file_duration_ms()
        if duration_ms <= 0:
            return
        if abs(segment.duration_ms - duration_ms) < 300:
            return

        global_before = self._current_global_position()
        segment.duration_ms = duration_ms
        self._rebuild_timeline()
        self.timeline_slider.setValue(min(global_before, self.timeline_slider.maximum()))
        self._update_time_label(global_before)

    def _sync_timeline(self) -> None:
        if not self.segments:
            self.play_button.setText("Play")
            return

        self._finalize_pending_seek()
        self._update_current_segment_duration()

        if 0 <= self.current_index < len(self.segments):
            segment = self.segments[self.current_index]
            if segment.kind == "media" and self.pending_seek_ms is None:
                duration_ms = self._current_file_duration_ms()
                position_ms = self._current_file_position_ms()
                if duration_ms > 0 and position_ms >= max(0, duration_ms - 120):
                    if self.last_end_handled_index != self.current_index:
                        self.last_end_handled_index = self.current_index
                        next_playable = self._find_next_playable_index(self.current_index)
                        if next_playable is not None:
                            self._load_segment(next_playable, seek_ms=0, should_play=True)
                        else:
                            self._pause_playback()
                        return
                else:
                    self.last_end_handled_index = -1

        if self.timeline_slider.isSliderDown():
            self.play_button.setText("Pause" if self._is_playing() else "Play")
            return

        global_ms = self._current_global_position()
        self.timeline_slider.blockSignals(True)
        self.timeline_slider.setValue(max(0, min(global_ms, self.timeline_slider.maximum())))
        self.timeline_slider.blockSignals(False)
        self._update_time_label(global_ms)
        self.play_button.setText("Pause" if self._is_playing() else "Play")

    def _find_next_playable_index(self, index: int) -> Optional[int]:
        for next_index in range(index + 1, len(self.segments)):
            seg = self.segments[next_index]
            if seg.kind == "media" and seg.path is not None:
                return next_index
        return None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.loader_thread is not None and self.loader_thread.isRunning():
            self.loader_thread.quit()
            self.loader_thread.wait(2000)

        if hasattr(self, "mpv_player"):
            try:
                self.mpv_player.terminate()
            except Exception:
                pass

        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Homecam timeline player")
    parser.add_argument("root", nargs="?", help="Root folder that contains minute mp4 files")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = HomecamPlayerWindow()
    window.show()

    if args.root:
        window.load_folder(Path(args.root))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
