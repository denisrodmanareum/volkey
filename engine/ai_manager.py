"""Unified AI model lifecycle manager for volky-bot.

Manages loading, health-checking, and proxying for all 4 foundation models
across the 3-layer architecture (MOIRAI → Chronos+PatchTST → Lag-Llama).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

from engine.model_config import (
    ENABLE_CHRONOS,
    ENABLE_LAG_LLAMA,
    ENABLE_MOIRAI,
    ENABLE_PATCHTST,
    MIN_AI_CONFIDENCE,
    auto_detect_device,
)
from engine.moirai_scanner import MoiraiBatchTask, MoiraiScanResult, MoiraiScanner
from engine.risk_gate import LagLlamaRisk, RiskResult
from engine.signal_generator import BlendedSignal, SignalBlender

logger = logging.getLogger(__name__)


class AIModelManager:
    """Single entry point for all AI model operations."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        device = cfg.get("device", "auto")
        self._enabled = cfg.get("enabled", True)

        # Layer 1
        self._moirai_task: Optional[MoiraiBatchTask] = None
        if ENABLE_MOIRAI and self._enabled:
            self._moirai_task = MoiraiBatchTask(
                MoiraiScanner(device=device),
                top_n=cfg.get("anomaly_top_n", 20),
            )

        # Layer 2
        self._signal_blender: Optional[SignalBlender] = None
        if (ENABLE_CHRONOS or ENABLE_PATCHTST) and self._enabled:
            self._signal_blender = SignalBlender(device=device)

        # Layer 3
        self._risk_gate: Optional[LagLlamaRisk] = None
        if ENABLE_LAG_LLAMA and self._enabled:
            self._risk_gate = LagLlamaRisk(device=device)

        self._load_status: dict[str, bool] = {}
        self._load_errors: dict[str, str] = {}
        self._loaded_at: float = 0

    def load_all(self) -> dict[str, bool]:
        """Load all enabled models. Returns status dict."""
        if not self._enabled:
            logger.info("ai_manager: disabled by config")
            return {}

        t0 = time.time()
        status = {}

        # Layer 1: MOIRAI
        if self._moirai_task is not None:
            ok = self._moirai_task.load()
            status["moirai"] = ok
            if not ok:
                self._load_errors["moirai"] = "load_failed"
                logger.warning("ai_manager: MOIRAI load failed")

        # Layer 2: Chronos + PatchTST
        if self._signal_blender is not None:
            blend_status = self._signal_blender.load()
            status.update(blend_status)
            for k, v in blend_status.items():
                if not v:
                    self._load_errors[k] = "load_failed"

        # Layer 3: Lag-Llama
        if self._risk_gate is not None:
            self._risk_gate.load()
            ok = self._risk_gate.is_loaded
            status["lag_llama"] = ok
            if not ok:
                self._load_errors["lag_llama"] = "load_failed"

        self._load_status = status
        self._loaded_at = time.time()
        elapsed = time.time() - t0
        logger.info("ai_manager: loaded in %.1fs — %s", elapsed, status)
        return status

    # ─── Layer 1 proxies ─────────────────────────────────────

    def submit_moirai_scan(self, coin_closes: dict[str, list[float]]) -> None:
        if self._moirai_task is not None and self._moirai_task.is_loaded:
            self._moirai_task.submit_scan(coin_closes)

    def run_moirai_scan_sync(self, coin_closes: dict[str, list[float]]) -> list[MoiraiScanResult]:
        if self._moirai_task is not None and self._moirai_task.is_loaded:
            return self._moirai_task.submit_scan_sync(coin_closes)
        return []

    def get_moirai_candidates(self) -> list[MoiraiScanResult]:
        if self._moirai_task is None:
            return []
        return self._moirai_task.get_latest_candidates()

    @property
    def moirai_scan_age(self) -> float:
        if self._moirai_task is None:
            return float("inf")
        return self._moirai_task.last_scan_age

    # ─── Layer 2 proxies ─────────────────────────────────────

    def generate_signal(
        self,
        closes: list[float],
        ohlcv_5m: Optional[np.ndarray] = None,
        timestamp_ms: int = 0,
    ) -> Optional[BlendedSignal]:
        if self._signal_blender is None or not self._signal_blender.any_loaded:
            return None
        return self._signal_blender.generate_signal(closes, ohlcv_5m, timestamp_ms)

    # ─── Layer 3 proxies ─────────────────────────────────────

    def check_risk(
        self,
        closes: list[float],
        timeframe: str = "5m",
    ) -> Optional[RiskResult]:
        if self._risk_gate is None or not self._risk_gate.is_loaded:
            return None
        return self._risk_gate.assess_risk(closes, timeframe)

    # ─── Status ──────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "models": self._load_status,
            "errors": self._load_errors,
            "loaded_at": self._loaded_at,
            "moirai_scan_age_s": round(self.moirai_scan_age, 1),
        }
