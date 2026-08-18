"""
Web 端实盘/模拟盘切换支持。

模式存储在 reports/paper/web_mode.json：
- "real": 显示币安实盘 API 数据
- "paper": 显示 paper ledger 模拟数据

不需要重启 daemon — 切换是显示层面。
"""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
MODE_PATH = ROOT / "reports" / "paper" / "web_mode.json"


def get_mode() -> str:
    """读取当前模式。默认 'real'（兼容历史）。"""
    try:
        if MODE_PATH.exists():
            data = json.loads(MODE_PATH.read_text())
            m = data.get("mode", "real")
            if m in ("real", "paper"):
                return m
    except Exception as ex:
        log.warning("web_mode.json read failed: %s", ex)
    return "real"


def set_mode(mode: str) -> None:
    """设置当前模式。"""
    if mode not in ("real", "paper"):
        raise ValueError(f"invalid mode: {mode}")
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODE_PATH.write_text(json.dumps({
        "mode": mode,
        "updated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }, indent=2))
    log.info("web mode set to: %s", mode)


def resolve_mode(arg: str | None = None) -> str:
    """根据 query 参数或 cookie 决定模式。优先级: arg > saved > default 'real'."""
    if arg in ("real", "paper"):
        return arg
    return get_mode()
