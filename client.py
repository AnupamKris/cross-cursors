import argparse
import json
import socket
import sys
from typing import Optional

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


def run_client(host: str, port: int) -> None:
    mouse = Controller()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    buffer = b""
    print(f"[client] connected to {host}:{port}")

    try:
        while True:
            chunk = sock.recv(4096)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-cursors mouse client")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    args = parser.parse_args()
    try:
        run_client(args.host, args.port)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"[client] error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

