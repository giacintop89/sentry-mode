#!/usr/bin/env python3
"""Install the checksum-pinned YOLOX Nano ONNX model used by OpenCV DNN."""

import hashlib
import tempfile
import urllib.request
from pathlib import Path

URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"
SHA256 = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"


def main():
    target = Path(__file__).resolve().parents[1] / "models/yolox/yolox_nano.onnx"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == SHA256:
        print("Object detector model is already installed.")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
            temporary = Path(out.name)
            with urllib.request.urlopen(URL, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
            raise ValueError("Detector download checksum mismatch; existing model left unchanged.")
        temporary.replace(target)
        print("Installed YOLOX Nano:", target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
