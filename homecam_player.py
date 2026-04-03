from __future__ import annotations

import argparse
import bisect
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
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

DEFAULT_SEGMENT_MS = 60_000
FILE_TIME_RE = re.compile(r"(?P<mm>\d{2})M(?P<ss>\d{2})S(?:_(?P<ts>\d+))?", re.IGNORECASE)
FOLDER_TIME_RE = re.compile(r"^\d{10}$")
SETTINGS_ORG = "homecam-player"
SETTINGS_APP = "timeline-player"
SETTINGS_LAST_FOLDER = "last_open_folder"
SETTINGS_VOLUME = "volume"
GAP_THRESHOLD_MS = 10_000


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
            self.setValue(value)
            self.sliderMoved.emit(value)
            self.sliderReleased.emit()
            event.accept()
            return
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


def _quick_mp4_header_check(file_path: Path) -> tuple[bool, str]:
    try:
        if file_path.stat().st_size <= 1024:
            return False, "File too small"

        with file_path.open("rb") as f:
            header = f.read(64)
        if b"ftyp" not in header:
            return False, "Missing MP4 ftyp header"

        return True, "ok"
    except OSError as exc:
        return False, f"Read error: {exc}"


def _probe_mp4_with_ffprobe(ffprobe_path: str, file_path: Path) -> tuple[bool, str, int]:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ffprobe error: {exc}", DEFAULT_SEGMENT_MS

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        reason = stderr if stderr else "ffprobe failed"
        return False, reason, DEFAULT_SEGMENT_MS

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "ffprobe output parse failed", DEFAULT_SEGMENT_MS

    streams = payload.get("streams", [])
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    if not has_video:
        return False, "No video stream", DEFAULT_SEGMENT_MS

    format_info = payload.get("format", {})
    duration_raw = format_info.get("duration")
    if duration_raw is None:
        return True, "ok (duration unknown)", DEFAULT_SEGMENT_MS

    try:
        duration_ms = max(1_000, int(float(duration_raw) * 1000))
    except (TypeError, ValueError):
        duration_ms = DEFAULT_SEGMENT_MS

    return True, "ok", duration_ms


def build_segment_index(
    root: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[Segment], list[ValidationIssue], str]:
    files = sorted(root.rglob("*.mp4"))
    total_files = len(files)
    ffprobe_path = shutil.which("ffprobe")
    validation_mode = "ffprobe" if ffprobe_path else "header"
    invalid_files: list[ValidationIssue] = []

    probed: list[Segment] = []
    for index, file_path in enumerate(files, start=1):
        parsed_dt = parse_source_datetime(file_path)
        if ffprobe_path:
            is_valid, reason, duration_ms = _probe_mp4_with_ffprobe(ffprobe_path, file_path)
        else:
            is_valid, reason = _quick_mp4_header_check(file_path)
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
        self.validation_issues: list[ValidationIssue] = []
        self.validation_mode = "unknown"
        self.loaded_root: Optional[Path] = None
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.playback_rate = 1.0
        self.volume_percent = self._load_saved_volume()
        self.unplayable_ranges: list[tuple[int, int, QColor]] = []
        self.virtual_position_ms: Optional[int] = None
        self.is_loading = False
        self.loader_thread: Optional[QThread] = None
        self.loader_worker: Optional[SegmentLoaderWorker] = None

        self._setup_ui()
        self._setup_player()
        self._setup_timer()

    def _setup_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        self.video_widget = QVideoWidget()
        layout.addWidget(self.video_widget, 1)

        self.timeline_slider = ClickableSlider(Qt.Horizontal)
        self.timeline_slider.setRange(0, 0)
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
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.audio_output.setVolume(self.volume_percent / 100.0)

        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)

    def _setup_timer(self) -> None:
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(120)
        self.ui_timer.timeout.connect(self._sync_timeline)
        self.ui_timer.start()

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
        self.player.pause()
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
            self.player.stop()
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
        return self.segments[self.current_index].start_ms + max(0, self.player.position())

    def _load_segment(self, index: int, seek_ms: int, should_play: bool) -> None:
        if index < 0 or index >= len(self.segments):
            return

        segment = self.segments[index]
        if segment.kind != "media" or segment.path is None:
            self.current_index = index
            self.virtual_position_ms = segment.start_ms + max(0, min(seek_ms, segment.duration_ms - 1))
            self.player.pause()
            if segment.kind == "gap":
                self.segment_label.setText("No recording in this time interval (timeline gap).")
            else:
                self.segment_label.setText("")
            return

        self.current_index = index
        self.virtual_position_ms = None
        self.pending_seek_ms = max(0, seek_ms)
        self.pending_should_play = should_play

        self.segment_label.setText("")
        self.player.setSource(QUrl.fromLocalFile(str(segment.path)))
        self.player.setPlaybackRate(self.playback_rate)

    def _seek_global(self, global_ms: int) -> None:
        if not self.segments:
            return

        idx, offset = self._map_global_to_segment(global_ms)
        should_play = self.player.playbackState() == QMediaPlayer.PlayingState

        if idx == self.current_index:
            segment = self.segments[idx]
            if segment.kind != "media":
                self.virtual_position_ms = segment.start_ms + offset
                self._update_time_label(self.virtual_position_ms)
                return
            self.player.setPosition(offset)
            if should_play:
                self.player.play()
            return

        self._load_segment(idx, seek_ms=offset, should_play=should_play)

    def _on_slider_preview(self, value: int) -> None:
        self._seek_global(value)
        self._update_time_label(value)

    def _on_slider_released(self) -> None:
        self._seek_global(self.timeline_slider.value())

    def _toggle_play(self) -> None:
        if not self.segments:
            QMessageBox.information(self, "No media", "Load a folder first.")
            return

        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.current_index < 0:
                first_playable = self._find_next_playable_index(-1)
                if first_playable is not None:
                    self._load_segment(first_playable, seek_ms=0, should_play=True)
            else:
                segment = self.segments[self.current_index]
                if segment.kind == "media":
                    self.player.play()
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
        if hasattr(self, "player"):
            self.player.setPlaybackRate(self.playback_rate)

    def _on_volume_changed(self, value: int) -> None:
        self.volume_percent = max(0, min(100, value))
        self.audio_output.setVolume(self.volume_percent / 100.0)
        self.volume_value_label.setText(f"{self.volume_percent}%")
        self.settings.setValue(SETTINGS_VOLUME, self.volume_percent)

    def _sync_timeline(self) -> None:
        if not self.segments:
            return

        if self.timeline_slider.isSliderDown():
            return

        global_ms = self._current_global_position()
        self.timeline_slider.blockSignals(True)
        self.timeline_slider.setValue(max(0, min(global_ms, self.timeline_slider.maximum())))
        self.timeline_slider.blockSignals(False)
        self._update_time_label(global_ms)

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.LoadedMedia:
            if self.pending_seek_ms is not None:
                self.player.setPosition(self.pending_seek_ms)
            if self.pending_should_play:
                self.player.play()
            else:
                self.player.pause()
            self.pending_seek_ms = None
            return

        if status == QMediaPlayer.EndOfMedia:
            next_playable = self._find_next_playable_index(self.current_index)
            if next_playable is not None:
                self._load_segment(next_playable, seek_ms=0, should_play=True)
            else:
                self.player.pause()

    def _find_next_playable_index(self, index: int) -> Optional[int]:
        for next_index in range(index + 1, len(self.segments)):
            seg = self.segments[next_index]
            if seg.kind == "media" and seg.path is not None:
                return next_index
        return None

    def _on_duration_changed(self, duration_ms: int) -> None:
        if self.current_index < 0 or self.current_index >= len(self.segments):
            return
        if duration_ms <= 0:
            return

        segment = self.segments[self.current_index]
        if abs(segment.duration_ms - duration_ms) < 300:
            return

        global_before = self._current_global_position()
        segment.duration_ms = duration_ms
        self._rebuild_timeline()
        self.timeline_slider.setValue(min(global_before, self.timeline_slider.maximum()))
        self._update_time_label(global_before)

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText("Pause" if state == QMediaPlayer.PlayingState else "Play")


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
