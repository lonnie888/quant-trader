"""Notifier: optional push notifications for daily results."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

FEISHU_DEFAULT_URL = None  # must be set via config or env var


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


class Notifier:
    def __init__(self, config: TelegramConfig):
        self.config = config

    def send(self, message: str) -> bool:
        if not self.config.enabled or not self.config.bot_token or not self.config.chat_id:
            log.debug("telegram notifier disabled or missing config")
            return False
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": self.config.chat_id, "text": message}, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("telegram send failed: %s", e)
            return False


class FeishuCardBuilder:
    """Build Feishu interactive card JSON."""

    @staticmethod
    def make_positions_check(today: str, total_unrealized_pct: float,
                             total_realized_pct: float, open_count: int,
                             closed_count: int, profitable: int,
                             positions: list[dict]) -> dict:
        elements = []
        elements.append({
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**📈 未实现**\n{total_unrealized_pct:+.2f}%"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**✅ 已实现**\n{total_realized_pct:+.2f}%"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**📦 持仓**\n{open_count} Open / {closed_count} Closed"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**🏆 浮盈**\n{profitable}/{open_count + closed_count}"}},
            ],
        })
        elements.append({"tag": "hr"})

        for r in positions:
            if "closed_pnl_pct_lev" in r or "current_pnl_pct_lev" in r:
                pnl = r["closed_pnl_pct_lev"] if r.get("closed_pnl_pct_lev") is not None else r.get("current_pnl_pct_lev", 0)
            else:
                pnl = r.get("pnl_pct_lev", 0) or 0
            pct = pnl * 100
            sym_short = r["symbol"].replace("/USDT:USDT", "")
            is_closed = r.get("exit_reason") is not None

            if is_closed:
                emoji = {"stop_loss": "🛑", "take_profit": "💰", "time": "⏰"}.get(r["exit_reason"], "❌")
                content = (
                    f"{emoji} **{sym_short}** — 已{r['exit_reason']}\n"
                    f"入场 `{r['entry_price']:.6f}` → 退出 `{r['exit_price']:.6f}`\n"
                    f"收益: **{pct:+.2f}%**"
                )
            else:
                dot = "🟢" if pnl > 0 else "🔴"
                content = (
                    f"{dot} **{sym_short}**\n"
                    f"入场 `{r['entry_price']:.6f}`  当前 `{r['last_close']:.6f}`\n"
                    f"收益: **{pct:+.2f}%**  |  剩余: {r.get('remaining_bars', '?')} bars"
                )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            })

        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": f"⏱ {today} 自动检查 · pump_pullback 策略"}],
        })

        if total_unrealized_pct + total_realized_pct > 10:
            header_template = "green"
        elif total_unrealized_pct + total_realized_pct > 0:
            header_template = "blue"
        else:
            header_template = "red"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text",
                              "content": f"📊 持仓检查 | {today}"},
                    "template": header_template,
                },
                "elements": elements,
            },
        }

    @staticmethod
    def make_daily_summary(as_of: str, gainers: list[tuple[str, float]],
                           accepted: int, blocked: int, open_pos: int) -> dict:
        """Build daily signal summary card. gainers: list of (symbol_short, pct_24h)."""
        if accepted > 0:
            header_template = "green"
            subtitle = f"🎯 {accepted} 个开仓信号"
        elif blocked > 0:
            header_template = "orange"
            subtitle = "⛔ 全部被风控阻挡"
        else:
            header_template = "blue"
            subtitle = "⏳ 无信号"

        gainer_lines = []
        for sym, pct in gainers[:15]:
            arrow = "🟢" if pct > 0 else "🔴"
            gainer_lines.append(f"{arrow} {sym} {pct:+.2f}%")
        if len(gainers) > 15:
            gainer_lines.append(f"... +{len(gainers) - 15} more")
        gainer_text = "\n".join(gainer_lines)

        elements = [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**🔥 涨幅TOP30**\n{gainer_text}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**📊 概况**\n{subtitle}"}},
                ],
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**✅ 开仓**\n{accepted}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**⛔ 风控阻挡**\n{blocked}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**📦 持仓**\n{open_pos} Open"}},
                ],
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                              "content": f"⏱ {as_of} · pump_pullback 策略 · watchlist 15min"}],
            },
        ]

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "📈 每日量化信号"},
                    "template": header_template,
                },
                "elements": elements,
            },
        }

    @staticmethod
    def make_position_close(symbol: str, exit_reason: str,
                            entry_price: float, exit_price: float,
                            pnl_pct_lev: float,
                            max_fav_pct: float, max_adv_pct: float) -> dict:
        reason_cn = {"stop_loss": "止损", "take_profit": "止盈", "time": "时间到期"}.get(exit_reason, exit_reason)
        sym_short = symbol.replace("/USDT:USDT", "")
        is_win = pnl_pct_lev > 0
        template = "green" if is_win else "red"
        emoji = "💰" if is_win else "🛑"
        sign = "+" if is_win else ""

        elements = [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**入场**\n`{entry_price:.6f}`"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**退出**\n`{exit_price:.6f}`"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**收益(杠杆)**\n{sign}{pnl_pct_lev * 100:.2f}%"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**最大浮盈/浮亏**\n+{max_fav_pct * 100:.2f}% / {max_adv_pct * 100:.2f}%"}},
                ],
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                              "content": f"退出原因: {reason_cn}"}],
            },
        ]

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text",
                              "content": f"{emoji} {sym_short} 已{reason_cn}"},
                    "template": template,
                },
                "elements": elements,
            },
        }


class FeishuNotifier:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or FEISHU_DEFAULT_URL

    def send(self, message: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            r = requests.post(self.webhook_url,
                              json={"msg_type": "text", "content": {"text": message}}, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("feishu send failed: %s", e)
            return False

    def send_card(self, card: dict) -> bool:
        if not self.webhook_url:
            return False
        try:
            r = requests.post(self.webhook_url, json=card, timeout=10)
            r.raise_for_status()
            log.info("feishu card sent")
            return True
        except Exception as e:
            log.warning("feishu card send failed: %s", e)
            return False