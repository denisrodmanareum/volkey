"""
volky-bot / engine / strategy_pool.py
전략 풀 관리 — 등록/조회/폐기/활성화
"""

import json
import time
from pathlib import Path
from typing import Optional

class StrategyPool:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(exist_ok=True)
        self.pool: list[dict] = self._load()

    def _load(self) -> list:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return []

    def _save(self):
        self.path.write_text(json.dumps(self.pool, ensure_ascii=False, indent=2))

    def add(self, strategy: dict):
        strategy["created_at"]  = time.strftime("%Y-%m-%dT%H:%M:%S")
        strategy["active"]      = False
        strategy["total_trades"]= 0
        strategy["wins"]        = 0
        strategy["total_pnl"]   = 0.0
        strategy["sharpe"]      = 0.0
        self.pool.append(strategy)
        self._save()

    def count(self) -> int:
        return len(self.pool)

    def get_all_stats(self) -> list:
        return [
            {
                "name":         s.get("name"),
                "win_rate":     s["wins"] / max(s["total_trades"], 1),
                "total_trades": s["total_trades"],
                "sharpe":       s.get("sharpe", 0.0),
                "active":       s.get("active", False),
            }
            for s in self.pool
        ]

    def get_stats(self, name: str) -> dict:
        for s in self.pool:
            if s["name"] == name:
                return {
                    "strategy_name": name,
                    "win_rate":      s["wins"] / max(s["total_trades"], 1),
                    "total_trades":  s["total_trades"],
                    "sharpe":        s.get("sharpe", 0.0),
                    "max_drawdown":  s.get("max_drawdown", 0.0),
                    "best_session":  s.get("best_session", "unknown"),
                    "worst_session": s.get("worst_session", "unknown"),
                    "failed_conditions": s.get("failed_conditions", []),
                }
        return {}

    def select_top(self, n: int = 3) -> list:
        ranked = sorted(
            [s for s in self.pool if s["total_trades"] >= 5],
            key=lambda s: s.get("sharpe", 0),
            reverse=True
        )
        return ranked[:n]

    def select_bottom(self, n: int = 1) -> list:
        ranked = sorted(
            [s for s in self.pool if s["total_trades"] >= 5],
            key=lambda s: s.get("sharpe", 0)
        )
        return ranked[:n]

    def select_safest(self, n: int = 1) -> list:
        ranked = sorted(
            self.pool,
            key=lambda s: s.get("max_drawdown", 1.0)
        )
        return ranked[:n]

    def set_active(self, strategies: list):
        names = {s["name"] for s in strategies}
        for s in self.pool:
            s["active"] = s["name"] in names
        self._save()

    def deactivate_all(self):
        for s in self.pool:
            s["active"] = False
        self._save()

    def set_trading_halt(self, halt: bool):
        # status.json에 거래 중단 플래그 기록
        status_file = self.path.parent / "status.json"
        try:
            status = json.loads(status_file.read_text()) if status_file.exists() else {}
        except Exception:
            status = {}
        status["trading_halt"] = halt
        status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2))

    def kill_underperformers(
        self, min_trades: int, min_win_rate: float, min_sharpe: float
    ) -> list[str]:
        killed = []
        survivors = []
        for s in self.pool:
            if s["total_trades"] < min_trades:
                survivors.append(s)
                continue
            wr = s["wins"] / max(s["total_trades"], 1)
            if wr < min_win_rate or s.get("sharpe", 0) < min_sharpe:
                killed.append(s["name"])
            else:
                survivors.append(s)
        self.pool = survivors
        if killed:
            self._save()
        return killed

    def update_trade_result(self, name: str, win: bool, pnl: float):
        for s in self.pool:
            if s["name"] == name:
                s["total_trades"] += 1
                if win:
                    s["wins"] += 1
                s["total_pnl"] = round(s.get("total_pnl", 0) + pnl, 4)
                self._save()
                return
