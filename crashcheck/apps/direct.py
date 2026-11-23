"""模式一: 直接覆盖写。

最朴素的更新方式: truncate 到 0 -> 写新内容。truncate 和 write 是两个独立
的数据面操作, 没有 fsync 时它们可能分别落盘: 截断落盘而写入丢失, 磁盘上
就只剩一个空文件 —— 旧版本已经被截断销毁, 新版本又没活下来。
"""
from __future__ import annotations

from typing import Dict, List

from ..model import FsRecorder
from .base import App, CheckResult, Violation

DATA = "/state.db"
V0 = "v0-baseline"
V1 = "v1-new-content"
TAG = "torn-overwrite"


class OverwriteBuggy(App):
    name = "overwrite_buggy"
    title = "直接覆盖写(无同步) —— 有缺陷"
    pattern = "直接覆盖写"
    description = (
        "truncate 到 0 后直接写新内容, 全程没有 fsync。截断与写入可能分别落盘, "
        "断电时若截断已落盘而写入丢失, 文件变成空文件, 旧版本同时已被销毁。"
    )
    known_defects = [{
        "tag": TAG,
        "summary": "覆盖写缺少 fsync, 断电后可能出现空文件/截断文件(旧版与新版都不在)",
    }]

    def initial_state(self) -> Dict[str, str]:
        return {DATA: V0}

    def run(self, fs: FsRecorder) -> None:
        fs.create(DATA) if not fs.exists(DATA) else None
        # 应用通过 open("w") 覆盖: 内核先截断, 再写入 —— 两条独立操作
        fs.truncate(DATA, 0)
        fs.write(DATA, V1, 0)
        fs.ack("update-to-v1")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        return dict(disk)

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        # ack 之前: 应用尚未确认, 旧版/新版/空文件都算"更新还没生效", 合法;
        # ack 之后: 确认过的新版本必须持久化, 任何其他内容都是缺陷
        allowed = [V1] if acked else [V0, V1, ""]
        self.require_file(recovered, DATA, allowed, violations, TAG,
                          "覆盖写一致性(旧版本或新版本, 已确认写入不丢)")
        return CheckResult(not violations, violations)


class OverwriteCorrect(App):
    name = "overwrite_correct"
    title = "直接覆盖写(写后 fsync) —— 正确写法"
    pattern = "直接覆盖写"
    description = (
        "同样 truncate+write, 但在向用户确认前 fsync 文件, 把截断和写入一起 "
        "推到磁盘。崩溃只可能发生在 fsync 之前(看到旧版本)或之后(看到新版本)。"
    )
    known_defects = []

    def initial_state(self) -> Dict[str, str]:
        return {DATA: V0}

    def run(self, fs: FsRecorder) -> None:
        fs.truncate(DATA, 0)
        fs.write(DATA, V1, 0)
        fs.fsync(DATA)
        fs.ack("update-to-v1")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        return dict(disk)

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        # fsync 之后才 ack: ack 前任何中间状态都合法, ack 后必须是新版本
        allowed = [V1] if acked else [V0, V1, ""]
        self.require_file(recovered, DATA, allowed, violations, TAG,
                          "覆盖写一致性(旧版本或新版本, 已确认写入不丢)")
        return CheckResult(not violations, violations)
