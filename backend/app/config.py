from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "叫号条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CALLSLIP_DB", str(ROOT / "data" / "callslip.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 号牌状态（仅中文）
TICKET_STATUSES = ("等候", "叫号中", "办理中", "过号", "已办结", "已作废")
ACTIVE_QUEUE = ("等候", "叫号中", "办理中", "过号")

# 跨种类选号次序口径（全局唯一，全系统只贯彻这一种）：
# 架构一「全局取号时刻先进先出」——在窗口可办种类交集内，按 (取号时刻, 号牌id) 升序叫最早者；
# 不得因种类偏好跳号；等候墙与叫号回执须能按同一谓词重放该次序。
ORDER_RULE_ID = "taken_at_fifo"
ORDER_RULE = "全局取号时刻先进先出（可办种类交集内按取号时刻升序，并列时刻以号牌id破）"
