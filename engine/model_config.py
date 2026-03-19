"""Centralized AI model configuration for volky-bot."""

from __future__ import annotations

from pathlib import Path

# Shared model weights directory (from main project)
MODELS_DIR = Path.home() / "Desktop/2026/models"

# Model paths
MOIRAI_PATH = MODELS_DIR / "moirai" / "moirai-moe-small"
LAG_LLAMA_CKPT = MODELS_DIR / "lag_llama" / "lag-llama.ckpt"
PATCHTST_DIR = MODELS_DIR / "patchtst"
CHRONOS_MODEL_ID = "amazon/chronos-2"

# Feature flags
ENABLE_MOIRAI = True
ENABLE_CHRONOS = True
ENABLE_PATCHTST = True
ENABLE_LAG_LLAMA = True

# MOIRAI Layer 1
MOIRAI_SCAN_INTERVAL = 600  # 10 min batch scan
MOIRAI_CONTEXT_LENGTH = 64
MOIRAI_PREDICTION_LENGTH = 6
MOIRAI_NUM_SAMPLES = 50
ANOMALY_TOP_N = 20

# Chronos + PatchTST Layer 2
CHRONOS_CONTEXT_WINDOW = 256
CHRONOS_PREDICTION_LENGTH = 6
CHRONOS_NUM_SAMPLES = 64
PATCHTST_CONTEXT_LENGTH = 64
SIGNAL_CHRONOS_WEIGHT = 0.35
SIGNAL_PATCHTST_WEIGHT = 0.65
MIN_AI_CONFIDENCE = 0.55

# Lag-Llama Layer 3
RISK_MIN_RR = 1.5
RISK_MIN_KELLY = 0.03
RISK_KELLY_MAX = 0.25
RISK_NUM_SAMPLES = 100


def auto_detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"
