"""Layer 1: MOIRAI-2 MoE batch scanner for volky-bot.

Scans 500+ coins every 10 minutes to find anomalous pre-surge patterns.
Runs in a background thread to avoid blocking the main trading loop.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from engine.model_config import (
    ANOMALY_TOP_N,
    MOIRAI_CONTEXT_LENGTH,
    MOIRAI_NUM_SAMPLES,
    MOIRAI_PATH,
    MOIRAI_PREDICTION_LENGTH,
    auto_detect_device,
)

logger = logging.getLogger(__name__)

_PATCH_SIZE = 16
_ANOMALY_PERCENTILE = 85.0


@dataclass
class MoiraiScanResult:
    symbol: str
    anomaly_score: float
    predicted_return: float
    volatility_estimate: float
    q10: float
    q50: float
    q90: float
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


class MoiraiScanner:
    """Zero-shot multi-coin scanner using MOIRAI-2 MoE."""

    def __init__(self, model_path: Optional[str] = None, device: str = "cpu"):
        self._model_path = Path(model_path or MOIRAI_PATH)
        # MOIRAI-2 MoE requires float64 → MPS incompatible, force CPU
        self._device_str = "cpu"
        self._module = None
        self._forecast_cls = None
        self._load_error: Optional[str] = None

    def load(self) -> None:
        try:
            import torch
            from hydra.utils import instantiate
            from safetensors.torch import load_file
            from uni2ts.model.moirai_moe import MoiraiMoEForecast
            from uni2ts.model.moirai_moe.module import MoiraiMoEModule
        except ImportError as e:
            self._load_error = f"import_failed: {e}"
            logger.warning("moirai: import failed — %s", e)
            return

        cfg_path = self._model_path / "config.json"
        weights_path = self._model_path / "model.safetensors"

        if not cfg_path.exists() or not weights_path.exists():
            self._load_error = f"weights_not_found: {self._model_path}"
            logger.warning("moirai: weights not found at %s", self._model_path)
            return

        try:
            cfg = json.loads(cfg_path.read_text())
            distr_output = instantiate(cfg["distr_output"])
            device = torch.device(self._device_str)

            module = MoiraiMoEModule(
                d_model=cfg["d_model"],
                d_ff=cfg["d_ff"],
                num_layers=cfg["num_layers"],
                patch_sizes=cfg["patch_sizes"],
                max_seq_len=cfg["max_seq_len"],
                attn_dropout_p=cfg["attn_dropout_p"],
                dropout_p=cfg["dropout_p"],
                scaling=cfg["scaling"],
                distr_output=distr_output,
            )
            state = load_file(str(weights_path))
            module.load_state_dict(state, strict=False)
            module.eval()
            module.to(device)

            self._module = module
            self._forecast_cls = MoiraiMoEForecast
            self._device = device
            n_params = sum(p.numel() for p in module.parameters()) / 1e6
            logger.info("moirai: loaded (%.1fM params, %s)", n_params, self._device_str)
        except Exception as e:
            self._load_error = str(e)
            logger.warning("moirai: load failed — %s", e)

    @property
    def is_loaded(self) -> bool:
        return self._module is not None

    def _predict_single(self, closes: np.ndarray) -> Optional[tuple[float, float, float, float]]:
        import torch
        try:
            ctx_len = MOIRAI_CONTEXT_LENGTH
            scale = float(np.abs(closes).mean()) + 1e-8
            ctx = (closes / scale)[-ctx_len:]
            if len(ctx) < ctx_len:
                ctx = np.concatenate([np.full(ctx_len - len(ctx), ctx[0]), ctx])

            past_target = torch.tensor(ctx, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
            past_observed = torch.ones(1, ctx_len, 1, dtype=torch.bool)
            past_is_pad = torch.zeros(1, ctx_len, dtype=torch.bool)

            forecast = self._forecast_cls(
                prediction_length=MOIRAI_PREDICTION_LENGTH,
                context_length=ctx_len,
                patch_size=_PATCH_SIZE,
                num_samples=MOIRAI_NUM_SAMPLES,
                target_dim=1,
                feat_dynamic_real_dim=0,
                past_feat_dynamic_real_dim=0,
                module=self._module,
            )
            forecast.to(self._device)

            with torch.no_grad():
                samples = forecast(
                    past_target.to(self._device),
                    past_observed.to(self._device),
                    past_is_pad.to(self._device),
                )

            s = samples.squeeze(0).cpu().numpy()
            last = s[:, -1]
            q10 = float(np.percentile(last, 10)) * scale
            q50 = float(np.percentile(last, 50)) * scale
            q90 = float(np.percentile(last, 90)) * scale
            std = float(np.std(s.mean(axis=1))) * scale
            return q10, q50, q90, std
        except Exception as e:
            logger.debug("moirai: predict failed — %s", e)
            return None

    def scan(self, coin_closes: dict[str, list[float]]) -> list[MoiraiScanResult]:
        if not self.is_loaded:
            return []

        results = []
        ts = int(time.time() * 1000)

        for symbol, closes in coin_closes.items():
            if len(closes) < 16:
                continue
            arr = np.array(closes, dtype=np.float32)
            pred = self._predict_single(arr)
            if pred is None:
                continue
            q10, q50, q90, std = pred
            current = float(arr[-1])
            score = self._anomaly_score(arr, q10, q50, q90, std)
            results.append(MoiraiScanResult(
                symbol=symbol,
                anomaly_score=score,
                predicted_return=(q50 / max(current, 1e-8)) - 1.0,
                volatility_estimate=std / max(current, 1e-8),
                q10=q10, q50=q50, q90=q90, timestamp=ts,
            ))

        results.sort(key=lambda r: r.anomaly_score, reverse=True)
        if results:
            logger.info("moirai: scanned %d coins, top=%s (%.3f)",
                        len(results), results[0].symbol, results[0].anomaly_score)
        return results

    def get_candidates(self, coin_closes: dict[str, list[float]], top_n: int = ANOMALY_TOP_N) -> list[MoiraiScanResult]:
        return self.scan(coin_closes)[:top_n]

    @staticmethod
    def _anomaly_score(closes: np.ndarray, q10: float, q50: float, q90: float, std: float) -> float:
        current = float(closes[-1])
        if current <= 0:
            return 0.0
        expected_ret = (q50 / current) - 1.0
        spread = max((q90 - q10) / current, 1e-8)
        confidence = float(np.clip(1.0 / (1.0 + spread * 20), 0, 1))
        upside_bias = max(float(np.clip((q10 / current - 1.0) * 100, -1, 1)), 0)
        momentum = 0.0
        if len(closes) >= 10:
            momentum = float(np.clip(np.std(closes[-3:]) / (np.abs(closes[-10:]).mean() + 1e-8) * 5, 0, 1))
        if expected_ret > 0:
            return float(np.clip(0.45 * np.clip(expected_ret * 30, 0, 1) + 0.30 * confidence + 0.15 * upside_bias + 0.10 * momentum, 0, 1))
        return 0.1 * momentum


class MoiraiBatchTask:
    """Runs MOIRAI scanner in a background thread, caching results."""

    def __init__(self, scanner: Optional[MoiraiScanner] = None, top_n: int = ANOMALY_TOP_N):
        self._scanner = scanner or MoiraiScanner()
        self._top_n = top_n
        self._candidates: list[MoiraiScanResult] = []
        self._last_scan_ts: float = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moirai")

    def load(self) -> bool:
        self._scanner.load()
        return self._scanner.is_loaded

    @property
    def is_loaded(self) -> bool:
        return self._scanner.is_loaded

    def submit_scan(self, coin_closes: dict[str, list[float]]) -> None:
        """Submit scan — runs in background thread."""
        if not self._scanner.is_loaded:
            return
        self._scanning = True
        self._executor.submit(self._run_scan, coin_closes)

    def submit_scan_sync(self, coin_closes: dict[str, list[float]]) -> list[MoiraiScanResult]:
        """Run scan synchronously and return results immediately."""
        if not self._scanner.is_loaded:
            return []
        self._run_scan(coin_closes)
        return list(self._candidates)

    def _run_scan(self, coin_closes: dict[str, list[float]]) -> None:
        try:
            self._candidates = self._scanner.get_candidates(coin_closes, self._top_n)
            self._last_scan_ts = time.time()
            self._scanning = False
        except Exception as e:
            self._scanning = False
            logger.warning("moirai batch: scan failed — %s", e)

    def get_latest_candidates(self, max_age_s: float = 660) -> list[MoiraiScanResult]:
        if time.time() - self._last_scan_ts > max_age_s:
            return []
        return list(self._candidates)

    @property
    def last_scan_age(self) -> float:
        return time.time() - self._last_scan_ts if self._last_scan_ts > 0 else float("inf")
