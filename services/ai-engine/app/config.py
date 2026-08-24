"""AI engine settings.

The engine holds almost no configuration of its own.  Zones, camera streams and
homographies are pulled from `GET /ingest/config` at boot and on reload, which is
what makes "restart the container" a safe operation mid-Wari — it comes back
with the *current* zone areas, not with whatever it was started with on Tuesday.

What lives here is only how to reach the core API, and how hard to work.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CrowdSource = Literal["live", "video", "sim"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "development"
    log_level: str = "INFO"
    service_name: str = "ai-engine"
    ai_engine_port: int = 8100

    # --- where the state lives ------------------------------------------
    core_api_url: str = "http://localhost:8000"
    ai_service_token: str = "dev-only-ai-service-token-change-me"
    publish_timeout_seconds: float = 5.0
    config_refresh_seconds: int = 300

    # --- pipeline --------------------------------------------------------
    # `sim` is the default because there is no temple CCTV on a laptop, and a
    # system that only works with hardware present is a system nobody can
    # develop against (Section 4/M2, SIMULATION MODE — REQUIRED).
    crowd_source: CrowdSource = "sim"
    # Section 4/M2 step 1: 30 FPS is wasted compute for crowd density.
    sample_fps: float = Field(default=2.0, gt=0, le=30)
    # Section 4/M2 step 4: aggregates are per zone, per 10-second window.
    window_seconds: int = Field(default=10, ge=1, le=300)
    # Stagnation is defined over 60s, which is longer than one window — the
    # tracker therefore keeps a rolling minute of velocity history.
    stagnation_window_seconds: int = Field(default=60, ge=10, le=600)

    # --- detection -------------------------------------------------------
    # Head-detection weights outperform full-body in dense crowds, so the path
    # is configurable rather than hard-coded (Section 4/M2 step 2).
    yolo_model_path: str = "yolov8n.pt"
    yolo_confidence: float = Field(default=0.25, ge=0.01, le=0.95)
    yolo_iou: float = Field(default=0.45, ge=0.1, le=0.9)
    yolo_device: str = "cpu"
    yolo_max_detections: int = Field(default=1000, ge=1, le=10000)
    #: Detected class index. 0 is `person` in the COCO models; a head-detection
    #: checkpoint typically has one class, also 0.
    yolo_person_class: int = 0

    # --- video source ----------------------------------------------------
    video_dir: str = "/data/videos"
    video_loop: bool = True

    # --- simulation ------------------------------------------------------
    sim_seed: int = 20260724
    #: Ashadhi Ekadashi 2026. The surge is centred here and ramps for three
    #: days either side.
    sim_ekadashi_date: str = "2026-07-25"
    sim_baseline_multiplier: float = Field(default=1.0, gt=0, le=20)

    # --- resilience ------------------------------------------------------
    #: Readings held in memory when the core API is unreachable.  At 40 zones
    #: every 10 seconds this is roughly four minutes of buffer, which covers an
    #: API redeploy.  Beyond that the oldest are dropped: during a surge the
    #: newest reading is the one that matters.
    buffer_max_readings: int = Field(default=1000, ge=10, le=100_000)

    @property
    def frame_interval_seconds(self) -> float:
        return 1.0 / self.sample_fps

    @property
    def ingest_url(self) -> str:
        return f"{self.core_api_url.rstrip('/')}/api/v1/ingest"

    def assert_production_safe(self) -> list[str]:
        problems: list[str] = []
        if "dev-only" in self.ai_service_token:
            problems.append("AI_SERVICE_TOKEN is still the development default")
        if self.crowd_source == "sim":
            problems.append("CROWD_SOURCE is 'sim' — the command centre would be showing invented numbers")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
