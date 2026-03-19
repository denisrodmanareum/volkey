"""
volky-bot / engine / telegram_bot.py

Telegram 알림 전송
- 진입/청산/긴급/일일 리포트
"""

import requests
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json

log = logging.getLogger("volky.telegram")
KST = timezone(timedelta(hours=9))

_cfg_file = Path(__file__).parent.parent / "config.json"
def _cfg() -> dict:
    if _cfg_file.exists():
        return json.loads(_cfg_file.read_text())
    return {}

def _send(text: str):
    cfg = _cfg()
    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        log.debug(f"[TG] 미설정 — 메시지 스킵: {text[:40]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"[TG] 전송 실패: {e}")


def notify_entry(symbol: str, side: str, entry: float,
                 sl: float, tp: float, pattern: str,
                 momentum: int, strategy: str, dry: bool = True):
    emoji = "🟢" if side == "long" else "🔴"
    mode  = "📄 페이퍼" if dry else "💰 실거래"
    now   = datetime.now(KST).strftime("%H:%M:%S")
    _send(
        f"{emoji} <b>진입</b> [{mode}] {now}\n"
        f"심볼: <code>{symbol}</code>\n"
        f"방향: {side.upper()}  진입가: {entry:,.4f}\n"
        f"SL: {sl:,.4f}  TP: {tp:,.4f}\n"
        f"패턴: {pattern}  모멘텀: {momentum}/100\n"
        f"전략: {strategy}"
    )


def notify_close(symbol: str, side: str, entry: float,
                 exit_price: float, pnl_pct: float,
                 reason: str, dry: bool = True):
    emoji = "✅" if pnl_pct > 0 else "❌"
    mode  = "📄 페이퍼" if dry else "💰 실거래"
    now   = datetime.now(KST).strftime("%H:%M:%S")
    _send(
        f"{emoji} <b>청산</b> [{mode}] {now}\n"
        f"심볼: <code>{symbol}</code>\n"
        f"진입: {entry:,.4f} → 청산: {exit_price:,.4f}\n"
        f"손익: <b>{pnl_pct:+.2f}%</b>  사유: {reason}"
    )


def notify_emergency(trigger: str, reason: str, action: str):
    _send(
        f"🚨 <b>긴급 트리거</b>\n"
        f"원인: {trigger}\n"
        f"상세: {reason}\n"
        f"조치: {action}"
    )


def notify_evolution(phase: int, new_strategies: list,
                     new_indicators: list, killed: list):
    lines = [
        f"🧬 <b>진화 사이클 완료</b> (Phase {phase})\n",
        f"신규 전략: {len(new_strategies)}개",
    ]
    for s in new_strategies[:3]:
        bt = s.get("backtest", {})
        lines.append(
            f"  • {s.get('name','?')} "
            f"Sharpe={bt.get('sharpe',0):.2f} "
            f"승률={bt.get('win_rate',0):.0%}"
        )
    if new_indicators:
        lines.append(f"신규 지표: {len(new_indicators)}개")
        for i in new_indicators:
            lines.append(f"  🔬 {i.get('name','?')}: {i.get('description','')}")
    if killed:
        lines.append(f"폐기: {', '.join(killed)}")
    _send("\n".join(lines))


def notify_daily_report(total_pnl: float, win_rate: float,
                        trades: int, active_strategies: list,
                        phase: int, total_trade_count: int):
    emoji = "📈" if total_pnl > 0 else "📉"
    _send(
        f"{emoji} <b>일일 리포트</b> {datetime.now(KST).strftime('%Y-%m-%d')}\n"
        f"손익: <b>{total_pnl:+.2f}%</b>  승률: {win_rate:.0%}  거래: {trades}건\n"
        f"진화 단계: Phase {phase} (누적 {total_trade_count}건)\n"
        f"활성 전략: {', '.join(s.get('name','?') for s in active_strategies[:3])}"
    )


def notify_surge_detected(symbol: str, pct: float, vol_mult: float,
                           pattern: str, momentum: int):
    _send(
        f"🔥 <b>급등 감지</b> <code>{symbol}</code>\n"
        f"변화율: {pct:+.1f}%  거래량: {vol_mult:.1f}x\n"
        f"패턴: {pattern}  모멘텀: {momentum}/100"
    )
