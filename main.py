import sys
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from pynput import keyboard, mouse


def _post_to_gui(callback: Callable[[], None]) -> None:
    """Schedule a callback on the Qt GUI thread."""
    QTimer.singleShot(0, callback)


class HotkeyService:
    """Registers global shortcuts using pynput and forwards to Qt safely."""

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_quit: Optional[Callable[[], None]] = None,
        toggle_combo: str = "<ctrl>+<alt>+o",
        quit_combo: str = "<ctrl>+<alt>+q",
    ) -> None:
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._listener = keyboard.GlobalHotKeys(
            {
                toggle_combo: self._wrap(self._on_toggle),
                quit_combo: self._wrap(self._on_quit) if self._on_quit else self._noop,
            }
        )
        self._listener.start()

    @staticmethod
    def _wrap(fn: Optional[Callable[[], None]]) -> Callable[[], None]:
        def _runner() -> None:
            if fn:
                _post_to_gui(fn)

        return _runner

    @staticmethod
    def _noop() -> None:
        return None

    def stop(self) -> None:
        self._listener.stop()


class CornerWatcher:
    """Listens to global mouse moves and triggers when entering bottom-left corner."""

    def __init__(self, threshold_px: int, on_enter: Callable[[], None]) -> None:
        self._threshold_px = max(1, threshold_px)
        self._on_enter = on_enter
        self._enabled = True
        self._in_corner = False
        self._listener = mouse.Listener(on_move=self._on_move)
        self._listener.start()

    def set_threshold(self, threshold_px: int) -> None:
        self._threshold_px = max(1, threshold_px)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        # Reset corner state when toggling to avoid stale latched value.
        self._in_corner = False

    def _on_move(self, x: float, y: float) -> None:
        if not self._enabled:
            return
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geometry = screen.geometry()
        in_corner = (
            x <= geometry.x() + self._threshold_px
            and y >= geometry.y() + geometry.height() - self._threshold_px
        )
        if in_corner and not self._in_corner:
            self._in_corner = True
            _post_to_gui(self._on_enter)
        elif not in_corner and self._in_corner:
            self._in_corner = False

    def stop(self) -> None:
        self._listener.stop()


class OverlayWindow(QWidget):
    """Full-screen-ish overlay that captures mouse events and blocks underlying apps."""

    def __init__(
        self,
        on_event: Callable[[str], None],
        on_escape: Callable[[], None],
        size: Optional[QSize] = None,
    ) -> None:
        super().__init__()
        self._event_callback = on_event
        self._escape_callback = on_escape
        self.setWindowTitle("Cross Cursors Overlay")
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._info_label = QLabel("Overlay idle")
        self._info_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        self._event_label = QLabel("Move the mouse to start capturing.")
        self._event_label.setStyleSheet("font-size: 14px;")
        self._resolution_label = QLabel("")
        self._resolution_label.setStyleSheet("font-size: 12px; color: #e0e0e0;")

        content = QWidget()
        content.setStyleSheet(
            "background-color: rgba(15, 15, 20, 150); color: white; border-radius: 10px;"
        )
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._info_label)
        content_layout.addWidget(self._event_label)
        content_layout.addWidget(self._resolution_label)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.addWidget(content, alignment=Qt.AlignTop)

        self._default_size = size or self._screen_size()
        self.set_overlay_size(self._default_size)

    def _screen_size(self) -> QSize:
        screen = QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        return geometry.size()

    def set_overlay_size(self, size: QSize) -> None:
        self.resize(size)
        self._center_on_primary()
        self._update_resolution_label()

    def _center_on_primary(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geometry = screen.availableGeometry()
        x = geometry.x() + (geometry.width() - self.width()) // 2
        y = geometry.y() + (geometry.height() - self.height()) // 2
        self.move(QPoint(x, y))

    def _update_resolution_label(self) -> None:
        self._resolution_label.setText(f"Overlay size: {self.width()} x {self.height()}")

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._info_label.setText("Overlay active • mouse input captured")

    def hide_overlay(self) -> None:
        self.hide()
        self._info_label.setText("Overlay idle")

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        text = (
            f"Move @ overlay ({pos.x():.0f}, {pos.y():.0f}) • "
            f"global ({global_pos.x():.0f}, {global_pos.y():.0f})"
        )
        self._info_label.setText(text)
        self._event_callback(text)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        button = event.button().name if hasattr(event.button(), "name") else str(event.button())
        text = (
            f"Press {button} at overlay ({pos.x():.0f}, {pos.y():.0f}) • "
            f"global ({global_pos.x():.0f}, {global_pos.y():.0f})"
        )
        self._event_label.setText(text)
        self._event_callback(text)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position()
        global_pos = event.globalPosition()
        button = event.button().name if hasattr(event.button(), "name") else str(event.button())
        text = (
            f"Release {button} at overlay ({pos.x():.0f}, {pos.y():.0f}) • "
            f"global ({global_pos.x():.0f}, {global_pos.y():.0f})"
        )
        self._event_label.setText(text)
        self._event_callback(text)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta()
        text = f"Wheel delta x={delta.x()} y={delta.y()}"
        self._event_label.setText(text)
        self._event_callback(text)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self._escape_callback()
            event.accept()
        else:
            super().keyPressEvent(event)


class ControlWindow(QMainWindow):
    """Small control panel to manage the overlay and resolution."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cross Cursors Controller")
        self._hotkeys: Optional[HotkeyService] = None
        self._corner_watcher: Optional[CornerWatcher] = None

        default_size = self._screen_size()
        self._overlay = OverlayWindow(
            on_event=self._set_last_event, on_escape=self._hide_overlay, size=default_size
        )

        self._state_label = QLabel("Overlay OFF")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #c62828;")
        self._last_event_label = QLabel("Last event: none")
        self._last_event_label.setWordWrap(True)
        self._hotkey_hint = QLabel("Global shortcut: Ctrl+Alt+O (toggle) • Ctrl+Alt+Q (quit)")
        self._hotkey_hint.setStyleSheet("color: #666;")

        self._width_spin = QSpinBox()
        self._width_spin.setRange(200, 9999)
        self._width_spin.setValue(default_size.width())
        self._height_spin = QSpinBox()
        self._height_spin.setRange(200, 9999)
        self._height_spin.setValue(default_size.height())

        self._corner_checkbox = QCheckBox("Auto-activate when cursor hits bottom-left corner")
        self._corner_checkbox.setChecked(True)
        self._corner_size_spin = QSpinBox()
        self._corner_size_spin.setRange(10, 400)
        self._corner_size_spin.setValue(60)
        self._corner_size_spin.setSuffix(" px")

        size_grid = QGridLayout()
        size_grid.addWidget(QLabel("Width"), 0, 0)
        size_grid.addWidget(self._width_spin, 0, 1)
        size_grid.addWidget(QLabel("Height"), 1, 0)
        size_grid.addWidget(self._height_spin, 1, 1)
        size_grid.addWidget(QLabel("Corner trigger size"), 2, 0)
        size_grid.addWidget(self._corner_size_spin, 2, 1)

        apply_size_btn = QPushButton("Apply size")
        apply_size_btn.clicked.connect(self._apply_size)

        toggle_btn = QPushButton("Toggle overlay (Ctrl+Alt+O)")
        toggle_btn.clicked.connect(self._toggle_overlay)

        stop_btn = QPushButton("Quit app (Ctrl+Alt+Q)")
        stop_btn.clicked.connect(self.close)

        button_row = QHBoxLayout()
        button_row.addWidget(apply_size_btn)
        button_row.addWidget(toggle_btn)
        button_row.addWidget(stop_btn)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(self._state_label)
        layout.addWidget(self._last_event_label)
        layout.addWidget(self._hotkey_hint)
        layout.addLayout(size_grid)
        layout.addWidget(self._corner_checkbox)
        layout.addLayout(button_row)
        layout.addStretch()
        self.setCentralWidget(root)

        self._register_local_shortcuts()
        self._register_global_hotkeys()
        self._register_corner_watcher()
        self._apply_size()

    def _screen_size(self) -> QSize:
        screen = QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        return geometry.size()

    def _register_local_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Alt+O"), self, activated=self._toggle_overlay)
        QShortcut(QKeySequence("Ctrl+Alt+Q"), self, activated=self.close)

    def _register_global_hotkeys(self) -> None:
        self._hotkeys = HotkeyService(on_toggle=self._toggle_overlay, on_quit=self.close)

    def _register_corner_watcher(self) -> None:
        self._corner_watcher = CornerWatcher(
            threshold_px=self._corner_size_spin.value(), on_enter=self._show_overlay_from_corner
        )
        self._corner_checkbox.toggled.connect(self._on_corner_toggle)
        self._corner_size_spin.valueChanged.connect(self._on_corner_size_change)
        self._update_corner_state()

    def _apply_size(self) -> None:
        size = QSize(self._width_spin.value(), self._height_spin.value())
        self._overlay.set_overlay_size(size)
        self._set_last_event(f"Overlay resized to {size.width()} x {size.height()}")

    def _toggle_overlay(self) -> None:
        if self._overlay.isVisible():
            self._hide_overlay()
        else:
            self._show_overlay()

    def _show_overlay(self) -> None:
        self._overlay.show_overlay()
        self._state_label.setText("Overlay ON")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #2e7d32;")

    def _hide_overlay(self) -> None:
        self._overlay.hide_overlay()
        self._state_label.setText("Overlay OFF")
        self._state_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #c62828;")

    def _set_last_event(self, message: str) -> None:
        self._last_event_label.setText(f"Last event: {message}")
        # Also log to stdout for external piping/forwarding if desired.
        print(message)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._hotkeys:
            self._hotkeys.stop()
        if self._corner_watcher:
            self._corner_watcher.stop()
        self._overlay.hide()
        super().closeEvent(event)

    def _on_corner_toggle(self, checked: bool) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_enabled(checked)

    def _on_corner_size_change(self, value: int) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_threshold(value)

    def _update_corner_state(self) -> None:
        if self._corner_watcher:
            self._corner_watcher.set_threshold(self._corner_size_spin.value())
            self._corner_watcher.set_enabled(self._corner_checkbox.isChecked())

    def _show_overlay_from_corner(self) -> None:
        if not self._overlay.isVisible():
            self._show_overlay()
            self._set_last_event("Auto-activated from bottom-left corner trigger")


def main() -> None:
    app = QApplication(sys.argv)
    controller = ControlWindow()
    controller.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
