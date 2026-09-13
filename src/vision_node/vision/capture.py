from pathlib import Path

from vision_node.hardware.camera import Camera


def capture_image(camera: Camera, output: Path) -> None:
    with camera:
        # Discard initial frames to allow exposure to settle.
        for _ in range(5):
            frame = camera.capture_frame()
        camera.save_frame(frame, str(output))
