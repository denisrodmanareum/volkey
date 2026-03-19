"""Layer 3: Lag-Llama probabilistic risk gate for volky-bot.

Blocks risky entries based on q10/q50/q90 confidence intervals
and adjusts position size via Kelly criterion.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from engine.model_config import (
    LAG_LLAMA_CKPT,
    RISK_KELLY_MAX,
    RISK_MIN_KELLY,
    RISK_MIN_RR,
    RISK_NUM_SAMPLES,
    auto_detect_device,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskResult:
    q10: float
    q50: float
    q90: float
    expected_return: float
    risk_reward: float
    kelly_fraction: float
    confidence: float

    def should_block(
        self,
        min_rr: float = RISK_MIN_RR,
        min_kelly: float = RISK_MIN_KELLY,
    ) -> tuple[bool, str]:
        if self.risk_reward < min_rr:
            return True, f"rr_low:{self.risk_reward:.2f}<{min_rr}"
        if self.kelly_fraction < min_kelly:
            return True, f"kelly_low:{self.kelly_fraction:.3f}<{min_kelly}"
        if self.expected_return < -0.005:
            return True, f"neg_return:{self.expected_return:.4f}"
        return False, "ok"

    def position_size_usdt(self, base_usdt: float, max_mult: float = 2.0) -> float:
        """Scale position size by Kelly fraction."""
        if self.kelly_fraction <= 0:
            return base_usdt * 0.5
        mult = min(self.kelly_fraction / 0.10, max_mult)  # 10% kelly = 1x base
        return base_usdt * max(mult, 0.3)


class LagLlamaRisk:
    """Lag-Llama probabilistic risk assessment."""

    def __init__(self, ckpt_path: Optional[str] = None, device: str = "auto"):
        self._ckpt_path = Path(ckpt_path or LAG_LLAMA_CKPT)
        self._device_str = auto_detect_device() if device == "auto" else device
        self._predictor = None
        self._load_error: Optional[str] = None

    def load(self) -> None:
        if not self._ckpt_path.exists():
            self._load_error = f"ckpt_not_found: {self._ckpt_path}"
            logger.warning("lag_llama: checkpoint not found at %s", self._ckpt_path)
            return

        try:
            import torch
            from lag_llama.gluon.estimator import LagLlamaEstimator
        except ImportError as e:
            self._load_error = f"import_failed: {e}"
            logger.warning("lag_llama: import failed — %s", e)
            return

        try:
            ckpt = torch.load(str(self._ckpt_path), map_location="cpu", weights_only=False)
            hparams = ckpt.get("hyper_parameters", {})
            model_kwargs = hparams.get("model_kwargs", {})

            estimator = LagLlamaEstimator(
                ckpt_path=str(self._ckpt_path),
                prediction_length=6,
                context_length=model_kwargs.get("context_length", hparams.get("context_length", 32)),
                input_size=model_kwargs.get("input_size", 1),
                n_layer=model_kwargs.get("n_layer", 8),
                n_embd_per_head=model_kwargs.get("n_embd_per_head", 16),
                n_head=model_kwargs.get("n_head", 9),
                scaling=model_kwargs.get("scaling", "robust"),
                time_feat=model_kwargs.get("time_feat", True),
                rope_scaling=model_kwargs.get("rope_scaling", None),
                device=torch.device(self._device_str),
                batch_size=1,
            )
            self._num_samples = RISK_NUM_SAMPLES

            lightning_module = estimator.create_lightning_module()
            transformation = estimator.create_transformation()
            self._predictor = estimator.create_predictor(transformation, lightning_module)

            logger.info("lag_llama: loaded (%s)", self._device_str)
        except Exception as e:
            self._load_error = str(e)
            logger.warning("lag_llama: load failed — %s", e)

    @property
    def is_loaded(self) -> bool:
        return self._predictor is not None

    def assess_risk(
        self,
        closes: list[float],
        timeframe: str = "5m",
    ) -> Optional[RiskResult]:
        if not self.is_loaded or len(closes) < 32:
            return None

        try:
            from gluonts.dataset.common import ListDataset
        except ImportError:
            return None

        try:
            import pandas as pd

            closes_arr = np.array(closes, dtype=np.float32)
            last_close = float(closes_arr[-1])
            freq_map = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h"}
            freq = freq_map.get(timeframe, "5min")

            dataset = ListDataset(
                [{"start": pd.Timestamp("2020-01-01", freq=freq), "target": closes_arr}],
                freq=freq,
            )

            forecasts = list(self._predictor.predict(dataset))
            if not forecasts:
                return None

            forecast = forecasts[0]
            samples = forecast.samples  # (num_samples, pred_len)
            last_step = samples[:, -1]

            q10 = float(np.percentile(last_step, 10))
            q50 = float(np.percentile(last_step, 50))
            q90 = float(np.percentile(last_step, 90))

            expected_ret = (q50 - last_close) / max(abs(last_close), 1e-8)
            upside = q90 - last_close
            downside = last_close - q10
            risk_reward = upside / max(downside, 1e-8) if downside > 0 else 10.0

            # Half-Kelly: f = (b*p - q) / (2*b)
            prob_win = float(np.mean(last_step > last_close))
            prob_loss = 1.0 - prob_win
            b = risk_reward
            kelly_raw = (b * prob_win - prob_loss) / (2 * b) if b > 0 else 0.0
            kelly_f = float(np.clip(kelly_raw, 0.0, RISK_KELLY_MAX))

            confidence = float(np.clip(1.0 - (q90 - q10) / max(abs(last_close), 1e-8) * 5, 0, 1))

            return RiskResult(
                q10=q10, q50=q50, q90=q90,
                expected_return=expected_ret,
                risk_reward=risk_reward,
                kelly_fraction=kelly_f,
                confidence=confidence,
            )
        except Exception as e:
            logger.debug("lag_llama: assess failed — %s", e)
            return None
