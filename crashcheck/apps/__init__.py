"""被测应用注册表: 4 类更新模式, 9 个应用(4 个带预埋缺陷, 5 个正确写法)。"""
from __future__ import annotations

from typing import List

from .base import App
from .direct import OverwriteBuggy, OverwriteCorrect
from .log_app import LogCheckpointBuggy, LogCheckpointCorrect
from .temprename import (
    TempRenameBuggy, TempRenameCorrect, TempRenamePartial,
)
from .wal import WalBuggy, WalCorrect

ALL_APPS: List[App] = [
    OverwriteBuggy(),
    OverwriteCorrect(),
    TempRenameBuggy(),
    TempRenamePartial(),
    TempRenameCorrect(),
    LogCheckpointBuggy(),
    LogCheckpointCorrect(),
    WalBuggy(),
    WalCorrect(),
]

APP_BY_NAME = {a.name: a for a in ALL_APPS}
