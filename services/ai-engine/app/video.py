"""Frame ingest: RTSP, a file on disk, or nothing (Section 4/M2 step 1).

Sampled at 2 FPS by default.  Thirty frames a second is thirty times the compute
for a number that changes on a ten-second window — and on the mini-PC that will
actually sit in a temple office, that difference is whether forty cameras run at
all.

OpenCV is imported lazily for the same reason as ultralytics: `CROWD_SOURCE=sim`
must not require it, and a camera that will not open must degrade to "offline"
rather than take the process down.

RTSP over TCP is forced.  UDP drops frames silently under load, and a silently
undercounting camera is worse than an offline one, because the map still looks
alive.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)


class VideoUnavailable(RuntimeError):
    """OpenCV is missing, or the stream would not open."""


@dataclass(frozen=True, slots=True)
class FrameInfo:
    width: int
    height: int
    fps: float
    frame_count: int | None


def _cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise VideoUnavailable(f"opencv is not installed: {exc}") from exc
    return cv2


def is_available() -> bool:
    try:
        _cv2()
    except VideoUnavailable:
        return False
    return True


class FrameSource:
    """A stream or file, sampled down to `sample_fps`."""

    def __init__(self, url: str, *, sample_fps: float | None = None, loop: bool | None = None) -> None:
        self.url = url
        self.sample_fps = sample_fps or settings.sample_fps
        self.loop = settings.video_loop if loop is None else loop
        self._capture: Any | None = None
        self._info: FrameInfo | None = None

    @property
    def info(self) -> FrameInfo | None:
        return self._info

    def open(self) -> None:
        cv2 = _cv2()
        if self.url.lower().startswith("rtsp"):
            # Must be set before the capture is constructed.
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

        capture = cv2.VideoCapture(self.url)
        if not capture.isOpened():
            capture.release()
            raise VideoUnavailable(f"could not open {_redact(self.url)}")

        # A one-frame buffer: on a live feed we want the newest frame, not the
        # oldest queued one.  A density reading from eight seconds ago that
        # claims to be current is a lie the operator cannot see.
        with_suppressed_error(lambda: capture.set(cv2.CAP_PROP_BUFFERSIZE, 1))

        native_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._info = FrameInfo(
            width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            fps=native_fps,
            frame_count=count or None,
        )
        self._capture = capture
        logger.info(
            "stream_opened",
            extra={
                "url": _redact(self.url),
                "width": self._info.width,
                "height": self._info.height,
                "native_fps": native_fps,
                "sample_fps": self.sample_fps,
            },
        )

    def frames(self) -> Iterator[Any]:
        """Yield frames at roughly `sample_fps`.

        For a file, frames are *skipped* rather than slept over, so a 30-minute
        recording replays in about a minute of wall time at 2 FPS — which is what
        makes "run a sample crowd video" a usable acceptance test rather than a
        half-hour wait.
        """
        if self._capture is None:
            self.open()
        capture = self._capture
        cv2 = _cv2()
        assert capture is not None

        native = (self._info.fps if self._info else 0.0) or self.sample_fps
        stride = max(1, int(round(native / self.sample_fps)))
        is_live = self.url.lower().startswith(("rtsp", "http"))
        interval = 1.0 / self.sample_fps
        next_at = time.monotonic()

        while True:
            ok, frame = capture.read()
            if not ok:
                if self.loop and not is_live and self._info and self._info.frame_count:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                logger.info("stream_ended", extra={"url": _redact(self.url)})
                return

            yield frame

            if is_live:
                # Live: pace by the clock and drop whatever arrived meanwhile.
                next_at += interval
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()
            else:
                for _ in range(stride - 1):
                    if not capture.grab():
                        break

    def grab_still(self) -> Any | None:
        """One frame, for the calibration page.  Opens and closes cleanly."""
        try:
            if self._capture is None:
                self.open()
            assert self._capture is not None
            ok, frame = self._capture.read()
            return frame if ok else None
        except VideoUnavailable:
            return None

    def close(self) -> None:
        if self._capture is not None:
            with_suppressed_error(self._capture.release)
            self._capture = None


def with_suppressed_error(action: Any) -> None:
    """Best-effort OpenCV call.  Backends disagree about what they support."""
    try:
        action()
    except Exception as exc:
        logger.debug("cv2_call_ignored", extra={"error": str(exc)})


def _redact(url: str) -> str:
    """RTSP URLs routinely carry credentials.  Never log them."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def sample_videos() -> list[Path]:
    """Video files sitting in `VIDEO_DIR`, for `CROWD_SOURCE=video`."""
    root = Path(settings.video_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov"})
