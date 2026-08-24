"""YOLOv8 person detection (Section 4/M2 step 2).

Person class only.  The model is never asked for anything else, and there is no
code path in this service that consumes any other class — that is a deliberate
narrowing, not an optimisation.

`ultralytics` is imported lazily and the import is allowed to fail.  Three
reasons, in order of how much they matter:

1. `CROWD_SOURCE=sim` is the default and needs none of it.  A 2 GB torch
   install must not be a prerequisite for running the tests or the demo.
2. On the day, a corrupt model file or a missing CUDA driver must degrade the
   system to "cameras offline, zones estimated" rather than crash the
   container into a restart loop.
3. The core API is unaffected either way. Passes keep working.

**Head weights**: in dense crowds, full-body detection collapses — the bodies
occlude each other and one detection covers four people.  Head-detection
checkpoints hold up much better past 4 p/m², which is exactly where the number
starts to matter, so `yolo_model_path` is configuration rather than a constant.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.logging import get_logger
from app.models import Detection

logger = get_logger(__name__)


class DetectorUnavailable(RuntimeError):
    """The vision stack is not installed or the weights would not load."""


class PersonDetector:
    """Thin wrapper over an Ultralytics model, loaded on first use."""

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path or settings.yolo_model_path
        self._model: Any | None = None
        self._failure: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def failure(self) -> str | None:
        return self._failure

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            self._failure = f"ultralytics is not installed: {exc}"
            raise DetectorUnavailable(self._failure) from exc

        try:
            model = YOLO(self.model_path)
            model.to(settings.yolo_device)
        except Exception as exc:
            self._failure = f"could not load {self.model_path}: {exc}"
            logger.error("detector_load_failed", extra={"model": self.model_path, "error": str(exc)})
            raise DetectorUnavailable(self._failure) from exc

        self._model = model
        self._failure = None
        logger.info(
            "detector_loaded",
            extra={"model": self.model_path, "device": settings.yolo_device, "confidence": settings.yolo_confidence},
        )

    def detect(self, frame: Any) -> list[Detection]:
        """One frame in, person boxes out.

        Returns an empty list rather than raising when the frame is unusable —
        a dropped frame is a gap in one window; an exception is a dead pipeline.
        """
        if self._model is None:
            self.load()

        try:
            results = self._model.predict(  # type: ignore[union-attr]
                source=frame,
                classes=[settings.yolo_person_class],
                conf=settings.yolo_confidence,
                iou=settings.yolo_iou,
                max_det=settings.yolo_max_detections,
                device=settings.yolo_device,
                verbose=False,
            )
        except Exception as exc:
            logger.warning("detector_predict_failed", extra={"error": str(exc)})
            return []

        detections: list[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                try:
                    x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                    confidence = float(box.conf[0])
                except (AttributeError, IndexError, TypeError, ValueError):
                    continue
                detections.append(Detection(x1=x1, y1=y1, x2=x2, y2=y2, confidence=confidence))

        return detections


def describe() -> dict[str, Any]:
    """What `/status` reports about detection, without forcing a model load."""
    try:
        import ultralytics

        version = getattr(ultralytics, "__version__", "unknown")
        available = True
    except ImportError:
        version = None
        available = False

    return {
        "available": available,
        "ultralytics_version": version,
        "model_path": settings.yolo_model_path,
        "device": settings.yolo_device,
        "confidence": settings.yolo_confidence,
        "person_class": settings.yolo_person_class,
        "note": (
            "Head-detection weights outperform full-body past ~4 p/m². "
            "Set YOLO_MODEL_PATH to a head checkpoint for dense zones."
        ),
    }
