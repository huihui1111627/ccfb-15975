"""模式四: 写前日志(Write-Ahead Log)。

WAL 协议: 先把"打算做什么"追加到日志并 fsync, 再改主数据文件, 崩溃后
重放已提交的日志条目。协议成立的前提是顺序:
    append(COMMIT) -> fsync(wal) -> 写主文件 -> ...
日志条目先于它保护的数据落盘才有意义。

预埋缺陷:
  1) COMMIT 记录 append 后没有 fsync 就去改主文件 —— 断电时主文件被
     截断/半写, 而能修复它的日志条目丢了 -> 主文件损坏且无法重放。
  2) 主文件更新本身无 fsync, 可能出现半截状态。
正确写法: 日志 fsync 先于任何主文件改动, 主文件改完再 fsync。
"""
from __future__ import annotations

from typing import Dict, List

from ..model import FsRecorder
from .base import App, CheckResult, Violation

MAIN = "/kv.db"
WAL = "/kv.wal"
V0 = "key=old"
V1 = "key=new"
ENTRY = "TXN1:SET key=new:COMMIT\n"
TAG_NOFSYNC = "wal-commit-not-durable"
TAG_TORN = "main-torn-write"


def _recover(disk: Dict[str, str]) -> (str, List[Violation]):
    """WAL 标准恢复: 有已提交事务就重放(主文件旧/空都没关系, 这正是 WAL
    存在的意义); 没有已提交事务时主文件必须保持完整旧版本。"""
    violations: List[Violation] = []
    main = disk.get(MAIN)
    wal = disk.get(WAL, "")
    committed = ENTRY.strip() in [ln.strip() for ln in wal.splitlines() if ln.strip()]
    if committed:
        # 日志已落盘: 无论主文件是什么状态, 重放都能得到新值 -> 健康
        return "key=new", violations
    # 没有已提交事务: 主文件必须是完整的旧版本;
    # 若主文件已被截断/半写而 COMMIT 丢了, 无法回滚也无法重放
    if main != V0:
        violations.append(Violation(
            tag=TAG_NOFSYNC,
            message="WAL 的 COMMIT 未落盘而主文件已被改成 %r: "
                    "日志没先于数据持久化, 损坏无法回滚也无法重放" % (main,),
            detail={"main": str(main), "wal": wal}))
        return "UNRECOVERABLE", violations
    return "key=old", violations


class WalBuggy(App):
    name = "wal_buggy"
    title = "写前日志(COMMIT 未同步就改主文件) —— 有缺陷"
    pattern = "写前日志"
    description = (
        "日志追加 COMMIT 后直接截断重写主文件, 中间没有 fsync(wal), "
        "主文件写完也不 fsync。断电时主文件已被截断而 COMMIT 丢失, "
        "恢复时既看不到完整新值, 也没有可重放的事务。"
    )
    known_defects = [
        {"tag": TAG_NOFSYNC,
         "summary": "WAL COMMIT 未 fsync 即修改主文件, 日志可能晚于/少于数据落盘"},
        {"tag": TAG_TORN,
         "summary": "主文件截断+写无 fsync, 可产生空/半截主文件"},
    ]

    def initial_state(self) -> Dict[str, str]:
        return {MAIN: V0, WAL: ""}

    def run(self, fs: FsRecorder) -> None:
        fs.append(WAL, ENTRY)      # 日志先写, 但……
        # 这里缺了 fs.fsync(WAL) —— WAL 的"W"形同虚设
        fs.truncate(MAIN, 0)
        fs.write(MAIN, V1, 0)
        fs.ack("txn1-committed")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        state, vios = _recover(disk)
        return {"__state__": state,
                "__violations__": "|".join(v.tag for v in vios)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        state = recovered.get("__state__", "MISSING")
        if state == "UNRECOVERABLE":
            violations.append(Violation(
                tag=TAG_NOFSYNC,
                message="恢复失败: 主文件损坏且 WAL 中无可用 COMMIT"))
        elif acked and state != "key=new":
            violations.append(Violation(
                tag=TAG_TORN,
                message="事务已确认但恢复状态为 %r, 已确认更新丢失" % state))
        return CheckResult(not violations, violations)


class WalCorrect(App):
    name = "wal_correct"
    title = "写前日志(COMMIT fsync 后再改主文件) —— 正确写法"
    pattern = "写前日志"
    description = (
        "append COMMIT -> fsync(wal) -> 截断重写主文件 -> fsync(主文件)。"
        "任何时刻断电: 日志在则重放得到新值, 日志不在则主文件原封不动。"
    )
    known_defects = []

    def initial_state(self) -> Dict[str, str]:
        return {MAIN: V0, WAL: ""}

    def run(self, fs: FsRecorder) -> None:
        fs.append(WAL, ENTRY)
        fs.fsync(WAL)                 # 先让日志不可丢失
        fs.truncate(MAIN, 0)
        fs.write(MAIN, V1, 0)
        fs.fsync(MAIN)
        fs.ack("txn1-committed")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        state, vios = _recover(disk)
        return {"__state__": state,
                "__violations__": "|".join(v.tag for v in vios)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        state = recovered.get("__state__", "MISSING")
        if state == "UNRECOVERABLE":
            violations.append(Violation(
                tag=TAG_NOFSYNC, message="恢复失败: %r" % state))
        elif acked and state != "key=new":
            violations.append(Violation(
                tag=TAG_TORN, message="已确认事务丢失: %r" % state))
        return CheckResult(not violations, violations)
