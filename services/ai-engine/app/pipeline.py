"""The pipeline: source -> metrics -> publish, once per window.

One `Pipeline` object owns the whole loop and is the thing `/status` reports on.
It runs the same shape regardless of source:

    every `window_seconds`:
        gather observations for every zone
        render nothing, store nothing, decide nothing
        POST them to the core API

The engine makes no decisions.  It does not know what 5 p/m² means, it does not
own the alert thresholds, and it cannot write to the database.  All three of
those live in the core API on purpose — so that this process, which is the one
running unsupervised vision code against forty RTSP streams, is also the one it
is safe to kill.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.detector import DetectorUnavailable, PersonDetector
from app.heatmap import summarise
from app.logging import get_logger, set_trace_id
from app.metrics import MotionHistory, aggregate, motions
from app.models import CameraSpec, TrackSample, ZoneObservation, ZoneSpec
from app.publisher import Publisher
from app.sim_engine import SimEngine
from app.tracker import GroundTracker
from app.video import FrameSource, VideoUnavailable, sample_videos

logger = get_logger(__name__)

#: How many windows of history `/status` and the acceptance script can read
#: back.  Six minutes at ten seconds — enough to show a surge developing,
#: short enough that it is unmistakably a debug buffer and not a datastore.
RECENT_WINDOWS = 36


@dataclass(slots=True)
class CameraRuntime:
    """Per-camera state for the video and live paths."""

    spec: CameraSpec
    source: FrameSource | None = None
    tracker: GroundTracker = field(default_factory=GroundTracker)
    status: str = "offline"
    last_frame_at: datetime | None = None
    frames_seen: int = 0
    error: str | None = None


class Pipeline:
    def __init__(self, publisher: Publisher | None = None) -> None:
        self.publisher = publisher or Publisher()
        self.zones: list[ZoneSpec] = []
        self.sim: SimEngine | None = None
        self.detector = PersonDetector()
        self.cameras: dict[str, CameraRuntime] = {}
        self.histories: dict[str, MotionHistory] = {}
        self.recent: deque[list[ZoneObservation]] = deque(maxlen=RECENT_WINDOWS)

        self.started_at: datetime | None = None
        self.windows_published = 0
        self.last_window_at: datetime | None = None
        self.last_error: str | None = None
        self.config_loaded_at: datetime | None = None
        self._stopping = asyncio.Event()

    # -- configuration ----------------------------------------------------
    async def load_config(self) -> bool:
        """Pull zones and calibrations from the core API.

        Returns False and keeps the previous configuration when the API is
        unreachable, so a core-api restart does not take the pipeline with it.
        """
        raw = await self.publisher.fetch_config()
        if raw is None:
            return False

        self.zones = [ZoneSpec.from_config(z) for z in raw.get("zones", [])]
        self.config_loaded_at = datetime.now(UTC)

        self.sim = SimEngine(
            self.zones,
            seed=settings.sim_seed,
            ekadashi=_parse_date(settings.sim_ekadashi_date),
            baseline_multiplier=settings.sim_baseline_multiplier,
        )

        window = timedelta(seconds=settings.stagnation_window_seconds)
        self.histories = {z.zone_id: MotionHistory(window) for z in self.zones}
        self._rebuild_cameras()

        uncalibrated = [c.name for z in self.zones for c in z.cameras if not c.is_calibrated]
        if uncalibrated and settings.crowd_source != "sim":
            # Loud, because a density figure from an uncalibrated camera is a
            # number with no units — and it would look exactly like a real one.
            logger.warning(
                "cameras_uncalibrated",
                extra={"count": len(uncalibrated), "cameras": uncalibrated[:10]},
            )

        logger.info(
            "config_loaded",
            extra={
                "zones": len(self.zones),
                "cameras": sum(len(z.cameras) for z in self.zones),
                "calibrated": sum(len(z.calibrated_cameras) for z in self.zones),
                "source": settings.crowd_source,
            },
        )
        return True

    def _rebuild_cameras(self) -> None:
        for runtime in self.cameras.values():
            if runtime.source is not None:
                runtime.source.close()
        self.cameras = {}
        if settings.crowd_source == "sim":
            return

        files = sample_videos() if settings.crowd_source == "video" else []
        index = 0
        for zone in self.zones:
            for camera in zone.cameras:
                url = camera.stream_url
                if settings.crowd_source == "video" and files:
                    # Round-robin the sample clips across cameras so a single
                    # test video exercises every zone.
                    url = str(files[index % len(files)])
                    index += 1
                runtime = CameraRuntime(spec=camera)
                if url:
                    runtime.source = FrameSource(url)
                else:
                    runtime.error = "no stream configured"
                self.cameras[camera.camera_id] = runtime

    # -- one window -------------------------------------------------------
    async def run_window(self, at: datetime | None = None) -> list[ZoneObservation]:
        """Produce and publish one window's observations."""
        set_trace_id()
        moment = at or datetime.now(UTC)

        if settings.crowd_source == "sim":
            observations = self._observe_sim(moment)
        else:
            observations = await asyncio.to_thread(self._observe_cameras, moment)

        self.recent.append(observations)
        self.last_window_at = moment

        result = await self.publisher.publish(observations)
        if result.ok:
            self.windows_published += 1
            self.last_error = None
        else:
            self.last_error = result.error

        await self._report_cameras()
        return observations

    def _observe_sim(self, at: datetime) -> list[ZoneObservation]:
        if self.sim is None:
            return []
        return self.sim.observe(at)

    def _observe_cameras(self, at: datetime) -> list[ZoneObservation]:
        """Blocking vision work.  Called in a thread so the API stays responsive.

        Each camera contributes tracked ground points for its zone; the zone's
        metrics are computed once from the union.  Two cameras covering the same
        corridor from opposite ends would double-count, which is why zones have
        one primary camera in the seed and why overlapping coverage is a
        calibration decision, not a modelling one.
        """
        samples_by_zone: dict[str, list[TrackSample]] = defaultdict(list)
        counts_by_zone: dict[str, list[int]] = defaultdict(list)
        cameras_by_zone: dict[str, int] = defaultdict(int)

        frames_per_window = max(1, int(settings.window_seconds * settings.sample_fps))

        for zone in self.zones:
            for camera in zone.cameras:
                runtime = self.cameras.get(camera.camera_id)
                if runtime is None or runtime.source is None or camera.homography is None:
                    continue

                frames = self._read_frames(runtime, frames_per_window)
                if not frames:
                    continue

                cameras_by_zone[zone.zone_id] += 1
                offset = timedelta(seconds=settings.window_seconds) / max(1, len(frames))
                for index, frame in enumerate(frames):
                    detections = self.detector.detect(frame)
                    counts_by_zone[zone.zone_id].append(len(detections))

                    points: list[tuple[float, float, float]] = []
                    for detection in detections:
                        px, py = detection.foot_point
                        ground = camera.homography.try_project(px, py)
                        if ground is not None:
                            points.append((ground[0], ground[1], detection.confidence))

                    stamp = at - timedelta(seconds=settings.window_seconds) + offset * (index + 1)
                    samples_by_zone[zone.zone_id].extend(runtime.tracker.update(points, stamp))

        observations: list[ZoneObservation] = []
        for zone in self.zones:
            history = self.histories.get(zone.zone_id)
            samples = samples_by_zone.get(zone.zone_id, [])
            counts = counts_by_zone.get(zone.zone_id, [])
            camera_count = cameras_by_zone.get(zone.zone_id, 0)

            if history is not None:
                history.extend(at, motions(samples))

            window = aggregate(
                samples,
                frame_counts=counts,
                history=history.motions() if history else (),
            )
            density = window.density(zone.area_m2)

            observations.append(
                ZoneObservation(
                    zone_id=zone.zone_id,
                    zone_code=zone.code,
                    person_count=window.person_count,
                    density=density,
                    observed_at=at,
                    flow_dx=window.flow_dx,
                    flow_dy=window.flow_dy,
                    stagnation_index=window.stagnation_index,
                    counterflow_ratio=window.counterflow_ratio,
                    confidence=_confidence(camera_count, window.tracks_considered, len(counts)),
                    camera_count=camera_count,
                    heat_cells=_heat_from_samples(samples, zone),
                )
            )

        # Section 4/M2 step 3: discard the track ids once flow is computed.
        for zone in self.zones:
            for camera in zone.cameras:
                runtime = self.cameras.get(camera.camera_id)
                if runtime is not None:
                    runtime.tracker.reset()

        return observations

    def _read_frames(self, runtime: CameraRuntime, count: int) -> list[Any]:
        frames: list[Any] = []
        try:
            if runtime.source is None:
                return frames
            iterator = runtime.source.frames()
            for _ in range(count):
                frames.append(next(iterator))
            runtime.status = "online"
            runtime.error = None
            runtime.frames_seen += len(frames)
            runtime.last_frame_at = datetime.now(UTC)
        except StopIteration:
            runtime.status = "degraded" if frames else "offline"
            runtime.error = "stream ended"
        except (VideoUnavailable, DetectorUnavailable) as exc:
            runtime.status = "offline"
            runtime.error = str(exc)
        except Exception as exc:
            runtime.status = "offline"
            runtime.error = f"{type(exc).__name__}: {exc}"
            logger.warning("camera_read_failed", extra={"camera": runtime.spec.name, "error": str(exc)})
        return frames

    async def _report_cameras(self) -> None:
        if not self.cameras:
            return
        await self.publisher.heartbeat(
            [
                {"camera_id": runtime.spec.camera_id, "status": runtime.status, "detail": runtime.error}
                for runtime in self.cameras.values()
            ]
        )

    # -- loop -------------------------------------------------------------
    async def run_forever(self) -> None:
        self.started_at = datetime.now(UTC)
        self._stopping.clear()

        while not await self.load_config():
            logger.warning("waiting_for_core_api", extra={"url": settings.core_api_url})
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=5.0)
                return
            except TimeoutError:
                continue

        interval = float(settings.window_seconds)
        last_config = datetime.now(UTC)

        while not self._stopping.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.run_window()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("window_failed")

            # Picks up new zones, re-surveyed areas and fresh calibrations
            # without a restart.  A failed refresh keeps the previous config
            # and retries on the next tick.
            now = datetime.now(UTC)
            due = (now - last_config).total_seconds() >= settings.config_refresh_seconds
            if due and await self.load_config():
                last_config = now

            elapsed = asyncio.get_running_loop().time() - started
            if elapsed > interval:
                # The window took longer than the window. Say so — silently
                # falling behind is how a "live" map ends up minutes stale.
                logger.warning("window_overran", extra={"took_seconds": round(elapsed, 2), "budget": interval})
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(0.0, interval - elapsed))
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stopping.set()
        for runtime in self.cameras.values():
            if runtime.source is not None:
                runtime.source.close()
        await self.publisher.aclose()

    # -- introspection ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        latest = self.recent[-1] if self.recent else []
        return {
            "source": settings.crowd_source,
            "started_at": self.started_at,
            "windows_published": self.windows_published,
            "last_window_at": self.last_window_at,
            "window_seconds": settings.window_seconds,
            "sample_fps": settings.sample_fps,
            "zones": len(self.zones),
            "config_loaded_at": self.config_loaded_at,
            "core_api": {
                "url": settings.core_api_url,
                "connected": self.publisher.is_connected,
                "last_success": self.publisher.last_success,
                "buffered_readings": self.publisher.buffered,
                "last_error": self.publisher.last_error or self.last_error,
            },
            "cameras": [
                {
                    "camera_id": runtime.spec.camera_id,
                    "name": runtime.spec.name,
                    "status": runtime.status,
                    "calibrated": runtime.spec.is_calibrated,
                    "frames_seen": runtime.frames_seen,
                    "error": runtime.error,
                }
                for runtime in self.cameras.values()
            ],
            "latest": summarise(latest),
        }

    def zone_series(self, zone_code: str) -> list[tuple[datetime, float]]:
        """Recent density for one zone, for the overlay page and the acceptance
        script.  In-memory and short — the durable series lives in Timescale."""
        code = zone_code.upper()
        return [
            (o.observed_at, o.density)
            for window in self.recent
            for o in window
            if o.zone_code == code
        ]

    def latest_for(self, zone_code: str) -> ZoneObservation | None:
        code = zone_code.upper()
        for window in reversed(self.recent):
            for observation in window:
                if observation.zone_code == code:
                    return observation
        return None


def _confidence(camera_count: int, tracks: int, frames: int) -> float:
    """How much to trust this window.

    Three things degrade it, and each is a real reason an operator should weigh
    the number lower: no camera reported, too few frames landed in the window,
    or nothing was tracked long enough to have a velocity.  A window with one
    camera and two frames is not a measurement, and it says so.
    """
    if camera_count == 0:
        return 0.0
    expected_frames = max(1, int(settings.window_seconds * settings.sample_fps))
    coverage = min(1.0, frames / expected_frames)
    motion = 1.0 if tracks >= 5 else 0.6 + 0.08 * tracks
    return round(min(1.0, 0.55 + 0.45 * coverage) * motion, 3)


def _heat_from_samples(samples: list[TrackSample], zone: ZoneSpec) -> tuple[tuple[float, float, float], ...]:
    """Bin ground positions into a 6x4 grid of local densities.

    Aggregate on the way in: the grid holds counts per cell, never positions, so
    what survives this function cannot place an individual anywhere.
    """
    if not samples:
        return ()

    xs = [s.x_m for s in samples]
    ys = [s.y_m for s in samples]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(1e-6, max_x - min_x)
    span_y = max(1e-6, max_y - min_y)

    cols, rows = 6, 4
    counts: dict[tuple[int, int], set[int]] = defaultdict(set)
    for sample in samples:
        col = min(cols - 1, int((sample.x_m - min_x) / span_x * cols))
        row = min(rows - 1, int((sample.y_m - min_y) / span_y * rows))
        counts[(col, row)].add(sample.track_id)

    cell_area = max(1.0, zone.area_m2 / (cols * rows))
    return tuple(
        ((col + 0.5) / cols, (row + 0.5) / rows, round(len(ids) / cell_area, 3))
        for (col, row), ids in sorted(counts.items())
    )


def _parse_date(value: str) -> Any:
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError:
        logger.warning("bad_ekadashi_date", extra={"value": value})
        return date(2026, 7, 25)
