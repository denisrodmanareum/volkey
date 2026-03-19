"""Layer 2: Chronos-2 + PatchTST signal generator for volky-bot.

Generates per-candidate directional signals by blending:
- Chronos-2: probabilistic close-price forecast
- PatchTST: patch-based OHLCV temporal pattern (if trained)
"""

from __future__ import annotations

import logging
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from engine.model_config import (
    CHRONOS_CONTEXT_WINDOW,
    CHRONOS_MODEL_ID,
    CHRONOS_NUM_SAMPLES,
    CHRONOS_PREDICTION_LENGTH,
    MIN_AI_CONFIDENCE,
    PATCHTST_CONTEXT_LENGTH,
    PATCHTST_DIR,
    SIGNAL_CHRONOS_WEIGHT,
    SIGNAL_PATCHTST_WEIGHT,
    auto_detect_device,
)

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    prob_up: float
    prob_down: float
    prob_neutral: float
    expected_return: float
    confidence: float
    source: str

    @property
    def direction(self) -> str:
        if self.prob_up > self.prob_down and self.prob_up > self.prob_neutral:
            return "UP"
        if self.prob_down > self.prob_up and self.prob_down > self.prob_neutral:
            return "DOWN"
        return "NEUTRAL"


@dataclass
class BlendedSignal:
    direction: str
    confidence: float
    expected_return: float
    prob_up: float
    prob_down: float
    sources: list[str]


# ─── Chronos-2 ───────────────────────────────────────────────

class ChronosSignal:
    """Simplified Chronos-2 forecast using only close prices."""

    def __init__(self, model_id: str = CHRONOS_MODEL_ID, device: str = "auto"):
        self._model_id = model_id
        self._device_str = auto_detect_device() if device == "auto" else device
        self._pipeline = None
        self._load_error: Optional[str] = None

    def load(self) -> None:
        try:
            from chronos import Chronos2Pipeline, ChronosPipeline
        except ImportError as e:
            self._load_error = f"chronos_import: {e}"
            logger.warning("chronos: import failed — %s", e)
            return

        import torch
        device_map = "cuda" if self._device_str == "cuda" else "cpu"
        dtype = torch.float32

        try:
            self._pipeline = Chronos2Pipeline.from_pretrained(
                self._model_id, device_map=device_map, torch_dtype=dtype,
            )
            logger.info("chronos: loaded Chronos2Pipeline (%s)", self._device_str)
        except Exception:
            try:
                self._pipeline = ChronosPipeline.from_pretrained(
                    self._model_id, device_map=device_map, torch_dtype=dtype,
                )
                logger.info("chronos: loaded ChronosPipeline fallback (%s)", self._device_str)
            except Exception as e:
                self._load_error = str(e)
                logger.warning("chronos: load failed — %s", e)

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    def predict(self, closes: list[float], timestamp_ms: int = 0) -> Optional[SignalResult]:
        if not self.is_loaded or len(closes) < 32:
            return None
        import torch
        try:
            # 수익률 기반 예측으로 편향 제거
            ctx = closes[-CHRONOS_CONTEXT_WINDOW:]
            arr = np.array(ctx, dtype=np.float64)
            returns = np.diff(np.log(np.maximum(arr, 1e-12)))  # log-returns
            if len(returns) < 16:
                return None

            context = torch.tensor(returns, dtype=torch.float32)
            forecast = self._pipeline.predict(
                context, CHRONOS_PREDICTION_LENGTH, num_samples=CHRONOS_NUM_SAMPLES,
            )
            samples = forecast.numpy()  # (num_samples, pred_len)
            if samples.ndim == 3:
                samples = samples.squeeze(0)

            # 예측된 수익률로 방향 판단 (0 기준, 편향 없음)
            cumulative_returns = samples.sum(axis=1)  # 전체 예측 구간 누적 수익률
            prob_up = float(np.mean(cumulative_returns > 0.0005))   # 0.05% 이상 상승
            prob_down = float(np.mean(cumulative_returns < -0.0005))  # 0.05% 이상 하락
            prob_neutral = max(0, 1.0 - prob_up - prob_down)

            expected_ret = float(np.median(cumulative_returns))
            confidence = max(prob_up, prob_down, prob_neutral)

            return SignalResult(
                prob_up=prob_up, prob_down=prob_down, prob_neutral=prob_neutral,
                expected_return=expected_ret, confidence=confidence, source="chronos",
            )
        except Exception as e:
            logger.debug("chronos: predict failed — %s", e)
            return None


# ─── PatchTST ────────────────────────────────────────────────

class PatchTSTSignal:
    """PatchTST OHLCV forecaster (locally trained)."""

    def __init__(self, timeframe: str = "15m", device: str = "auto"):
        self._timeframe = timeframe
        self._device_str = auto_detect_device() if device == "auto" else device
        self._model = None
        self._scaler: Optional[dict] = None
        self._load_error: Optional[str] = None
        self._ckpt_path = PATCHTST_DIR / f"patchtst_{timeframe}.pt"
        self._scaler_path = PATCHTST_DIR / f"scaler_{timeframe}.pkl"

    def load(self) -> None:
        if not self._ckpt_path.exists():
            self._load_error = f"no_checkpoint: {self._ckpt_path}"
            return
        try:
            import torch
            from transformers import PatchTSTConfig, PatchTSTForPrediction

            checkpoint = torch.load(str(self._ckpt_path), map_location="cpu", weights_only=False)
            cfg = checkpoint.get("config", {})
            config = PatchTSTConfig(
                num_input_channels=cfg.get("num_input_channels", 5),
                context_length=cfg.get("context_length", PATCHTST_CONTEXT_LENGTH),
                patch_length=cfg.get("patch_length", 8),
                patch_stride=cfg.get("patch_stride", 4),
                prediction_length=cfg.get("prediction_length", 6),
                d_model=cfg.get("d_model", 64),
                num_attention_heads=cfg.get("num_heads", 4),
                num_hidden_layers=cfg.get("num_layers", 3),
                ffn_dim=cfg.get("ffn_dim", 128),
                distribution_output="normal",
                loss="nll",
            )
            model = PatchTSTForPrediction(config)
            model.load_state_dict(checkpoint["model_state_dict"])
            model.eval()
            device = torch.device(self._device_str)
            model.to(device)
            self._model = model
            self._device = device

            if self._scaler_path.exists():
                with self._scaler_path.open("rb") as f:
                    self._scaler = pickle.load(f)

            logger.info("patchtst: loaded %s (%s)", self._timeframe, self._device_str)
        except Exception as e:
            self._load_error = str(e)
            logger.warning("patchtst: load failed — %s", e)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def predict(self, ohlcv: np.ndarray, timestamp_ms: int = 0) -> Optional[SignalResult]:
        """ohlcv: (T, 5) array of [open, high, low, close, volume]."""
        if not self.is_loaded:
            return None
        import torch
        try:
            ctx_len = PATCHTST_CONTEXT_LENGTH
            if len(ohlcv) < ctx_len:
                return None

            data = ohlcv[-ctx_len:].astype(np.float32).copy()
            data[:, 4] = np.log1p(data[:, 4])  # log-transform volume

            if self._scaler:
                mean = np.array(self._scaler["mean"], dtype=np.float32)
                std = np.array(self._scaler["std"], dtype=np.float32)
                std = np.where(std == 0, 1.0, std)
                data = (data - mean) / std

            past = torch.tensor(data, dtype=torch.float32).unsqueeze(0).to(self._device)
            with torch.no_grad():
                output = self._model(past_values=past)

            pred = output.prediction_outputs
            if isinstance(pred, tuple):
                pred = pred[0]
            if pred is None:
                return None

            close_idx = 3
            # Use all prediction steps for confidence
            pred_closes = pred[0, :, close_idx].cpu().numpy()
            last_past = float(past[0, -1, close_idx].cpu())

            # Direction from majority of forecast steps
            steps_up = int(np.sum(pred_closes > last_past))
            steps_down = int(np.sum(pred_closes < last_past))
            total_steps = len(pred_closes)

            prob_up = steps_up / total_steps
            prob_down = steps_down / total_steps
            prob_neutral = max(0, 1.0 - prob_up - prob_down)

            # Magnitude-weighted confidence
            last_pred = float(pred_closes[-1])
            pct_change = abs(last_pred - last_past) / max(abs(last_past), 1e-8)
            magnitude_boost = min(pct_change * 10, 0.15)  # up to 15% boost
            confidence = max(prob_up, prob_down) + magnitude_boost
            confidence = min(confidence, 0.95)

            return SignalResult(
                prob_up=prob_up, prob_down=prob_down, prob_neutral=prob_neutral,
                expected_return=last_pred - last_past, confidence=confidence,
                source=f"patchtst_{self._timeframe}",
            )
        except Exception as e:
            logger.debug("patchtst: predict failed — %s", e)
            return None


# ─── Blender ─────────────────────────────────────────────────

class SignalBlender:
    """Blends Chronos-2 + PatchTST into a single entry signal."""

    def __init__(self, device: str = "auto"):
        self._chronos = ChronosSignal(device=device)
        self._patchtst = PatchTSTSignal(timeframe="15m", device=device)

    def load(self) -> dict[str, bool]:
        self._chronos.load()
        self._patchtst.load()
        return {
            "chronos": self._chronos.is_loaded,
            "patchtst": self._patchtst.is_loaded,
        }

    @property
    def any_loaded(self) -> bool:
        return self._chronos.is_loaded or self._patchtst.is_loaded

    def generate_signal(
        self,
        closes: list[float],
        ohlcv_5m: Optional[np.ndarray] = None,
        timestamp_ms: int = 0,
    ) -> Optional[BlendedSignal]:
        if not self.any_loaded:
            return None

        preds: list[tuple[SignalResult, float]] = []

        # Chronos on close prices
        c_pred = self._chronos.predict(closes, timestamp_ms)
        if c_pred is not None:
            preds.append((c_pred, SIGNAL_CHRONOS_WEIGHT))

        # PatchTST on OHLCV
        if ohlcv_5m is not None:
            p_pred = self._patchtst.predict(ohlcv_5m, timestamp_ms)
            if p_pred is not None:
                preds.append((p_pred, SIGNAL_PATCHTST_WEIGHT))

        if not preds:
            return None

        total_w = sum(w for _, w in preds)
        prob_up = sum(p.prob_up * w for p, w in preds) / total_w
        prob_down = sum(p.prob_down * w for p, w in preds) / total_w
        exp_ret = sum(p.expected_return * w for p, w in preds) / total_w
        confidence = max(prob_up, prob_down)

        if prob_up > prob_down:
            direction = "LONG"
        elif prob_down > prob_up:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        return BlendedSignal(
            direction=direction,
            confidence=confidence,
            expected_return=exp_ret,
            prob_up=prob_up,
            prob_down=prob_down,
            sources=[p.source for p, _ in preds],
        )
