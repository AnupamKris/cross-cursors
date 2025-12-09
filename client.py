import argparse
import json
import socket
import sys
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QMessageBox,
)
from pynput.mouse import Button, Controller


def map_button(name: Optional[str]) -> Optional[Button]:
    if not name:
        return None
    name = name.lower()
    if "left" in name:
        return Button.left
    if "right" in name:
        return Button.right
    if "middle" in name or "wheel" in name:
        return Button.middle
    return None


class ClientThread(QThread):
    """Thread that runs the client connection."""

    status_changed = Signal(str)
    error_occurred = Signal(str)
    disconnected = Signal()

    def __init__(self, host: str, port: int, poll_ms: int) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._poll_ms = max(5, poll_ms)
        self._running = False
        self._sock: Optional[socket.socket] = None

    def run(self) -> None:
        mouse = Controller()
        self._running = True

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self._poll_ms / 1000.0)
            self._sock.connect((self._host, self._port))
            self.status_changed.emit(f"Connected to {self._host}:{self._port}")
            buffer = b""

            while self._running:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.status_changed.emit("Server closed connection")
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    handle_payload(mouse, payload)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self.disconnected.emit()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


def run_client(host: str, port: int, poll_ms: int) -> None:
    """Command-line version of the client."""
    mouse = Controller()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(poll_ms / 1000.0)
    sock.connect((host, port))
    buffer = b""
    print(f"[client] connected to {host}:{port}")

    try:
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                print("[client] server closed connection")
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                handle_payload(mouse, payload)
    finally:
        sock.close()


def handle_payload(mouse: Controller, payload: dict) -> None:
    ptype = payload.get("type")
    if ptype == "move":
        x_rel, y_rel = payload.get("x_rel"), payload.get("y_rel")
        sw, sh = payload.get("screen_width"), payload.get("screen_height")
        if None not in (x_rel, y_rel, sw, sh) and sw and sh:
            # Map relative coords to local primary screen size
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
            geo = screen.geometry() if screen else None
            if geo:
                nx = geo.x() + int(int(x_rel) / float(sw) * geo.width())
                ny = geo.y() + int(int(y_rel) / float(sh) * geo.height())
                mouse.position = (nx, ny)
                return
        x, y = payload.get("x"), payload.get("y")
        if x is not None and y is not None:
            mouse.position = (int(x), int(y))
    elif ptype == "press":
        btn = map_button(payload.get("button"))
        if btn:
            mouse.press(btn)
    elif ptype == "release":
        btn = map_button(payload.get("button"))
        if btn:
            mouse.release(btn)
    elif ptype == "scroll":
        dx = int(payload.get("dx", 0))
        dy = int(payload.get("dy", 0))
        mouse.scroll(dx, dy)


class ClientWindow(QMainWindow):
    """GUI window for the cross-cursors client."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cross Cursors Client")
        self._client_thread: Optional[ClientThread] = None

        # Create central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)

        # Status label
        self._status_label = QLabel("Disconnected")
        self._status_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #c62828;")
        layout.addWidget(self._status_label)

        # Host input
        host_layout = QHBoxLayout()
        host_layout.addWidget(QLabel("Host:"))
        self._host_input = QLineEdit("127.0.0.1")
        self._host_input.setPlaceholderText("Enter server host")
        host_layout.addWidget(self._host_input)
        layout.addLayout(host_layout)

        # Port input
        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("Port:"))
        self._port_input = QSpinBox()
        self._port_input.setRange(1024, 65535)
        self._port_input.setValue(8765)
        port_layout.addWidget(self._port_input)
        layout.addLayout(port_layout)

        # Buttons
        button_layout = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        self._disconnect_btn.setEnabled(False)
        button_layout.addWidget(self._connect_btn)
        button_layout.addWidget(self._disconnect_btn)
        layout.addLayout(button_layout)

        # Event log
        polling_layout = QHBoxLayout()
        polling_layout.addWidget(QLabel("Polling interval (ms):"))
        self._poll_input = QSpinBox()
        self._poll_input.setRange(5, 1000)
        self._poll_input.setValue(50)
        polling_layout.addWidget(self._poll_input)
        layout.addLayout(polling_layout)

        layout.addWidget(QLabel("Status:"))
        self._event_label = QLabel("Ready to connect")
        self._event_label.setWordWrap(True)
        self._event_label.setStyleSheet(
            "background-color: #2a2a2a; color: #f1f1f1; padding: 8px; border-radius: 6px;"
        )
        layout.addWidget(self._event_label)

        layout.addStretch()

        # Set minimum window size
        self.setMinimumSize(300, 200)

    def _on_connect_clicked(self) -> None:
        if self._client_thread and self._client_thread.isRunning():
            return

        host = self._host_input.text().strip()
        if not host:
            QMessageBox.warning(self, "Invalid Input", "Please enter a valid host address.")
            return

        port = self._port_input.value()
        poll_ms = self._poll_input.value()

        self._host_input.setEnabled(False)
        self._port_input.setEnabled(False)
        self._poll_input.setEnabled(False)
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._status_label.setText("Connecting...")
        self._status_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #f57c00;")
        self._event_label.setText(f"Connecting to {host}:{port}...")

        self._client_thread = ClientThread(host, port, poll_ms)
        self._client_thread.status_changed.connect(self._on_status_changed)
        self._client_thread.error_occurred.connect(self._on_error)
        self._client_thread.disconnected.connect(self._on_disconnected)
        self._client_thread.start()

    def _on_disconnect_clicked(self) -> None:
        if self._client_thread:
            self._client_thread.stop()
            self._client_thread.wait()
            self._on_disconnected()

    def _on_status_changed(self, message: str) -> None:
        self._event_label.setText(message)
        if "Connected" in message:
            self._status_label.setText("Connected")
            self._status_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #2e7d32;")

    def _on_error(self, error: str) -> None:
        self._event_label.setText(f"Error: {error}")
        self._status_label.setText("Error")
        self._status_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #c62828;")
        QMessageBox.critical(self, "Connection Error", f"Failed to connect:\n{error}")
        self._on_disconnected()

    def _on_disconnected(self) -> None:
        self._host_input.setEnabled(True)
        self._port_input.setEnabled(True)
        self._poll_input.setEnabled(True)
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._status_label.setText("Disconnected")
        self._status_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #c62828;")
        if self._client_thread:
            self._client_thread = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._client_thread and self._client_thread.isRunning():
            self._client_thread.stop()
            self._client_thread.wait()
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-cursors mouse client")
    parser.add_argument("--host", default=None, help="Server host (if not provided, GUI will open)")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    parser.add_argument("--poll-ms", type=int, default=50, help="Polling interval in milliseconds")
    parser.add_argument("--no-gui", action="store_true", help="Force command-line mode even without --host")
    args = parser.parse_args()

    # If host is provided, use CLI mode
    if args.host:
        try:
            run_client(args.host, args.port, args.poll_ms)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as exc:
            print(f"[client] error: {exc}", file=sys.stderr)
            sys.exit(1)
    # Otherwise, use GUI mode
    else:
        app = QApplication(sys.argv)
        window = ClientWindow()
        window.show()
        sys.exit(app.exec())


if __name__ == "__main__":
    main()

