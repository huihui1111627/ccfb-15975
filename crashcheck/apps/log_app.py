"""模式三: 追加日志 + 周期性检查点。

日志型服务把每条已确认的更新追加到 log 尾部, 周期性把累计状态固化成
checkpoint。崩溃后从最近 checkpoint 出发重放日志恢复。两个独立的薄弱点:

  日志面: append 没有 fsync 就向客户端返回成功 -> 已确认记录可能整段丢失
          (题目中"追加日志后没有同步"的事故)。
  检查点面: checkpoint 也走临时文件+rename, 同样存在"空 checkpoint 替换"和
          "目录项未同步"两类问题; 空 checkpoint 配合丢失的日志会回退到旧状态。

恢复规则: checkpoint 必须是完整合法的快照, 否则拒绝启动(服务起不来);
日志按行重放, 只接受完整行。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from ..model import FsRecorder
from .base import App, CheckResult, Violation

LOG = "/events.log"
CKPT = "/state.ckpt"
TMP = "/state.ckpt.tmp"
CKPT_V0 = "snapshot@0"
CKPT_V1 = "snapshot@2"      # checkpoint 固化了前两条事件后的状态
E1, E2, E3 = "evt-1", "evt-2", "evt-3"
TAG_LOST = "acked-append-lost"
TAG_CKPT = "torn-checkpoint"
TAG_MISSING = "ckpt-dirent-lost"


def _replay(disk: Dict[str, str]) -> Tuple[str, List[Violation]]:
    """从磁盘状态重放: 返回(恢复出的逻辑状态, 发现的损坏)。"""
    violations: List[Violation] = []
    snap = CKPT_V0
    if CKPT in disk:
        c = disk[CKPT]
        if c not in (CKPT_V0, CKPT_V1):
            violations.append(Violation(
                tag=TAG_CKPT,
                message="checkpoint 内容 %r 非法(空文件/半截快照), 服务无法启动" % c,
                detail={"path": CKPT, "content": c}))
            return "UNRECOVERABLE", violations
        snap = c
    elif TMP in disk and disk[TMP] not in ("", CKPT_V1):
        # 残留临时文件非法内容也视为异常信号(此处只记录, 不阻断)
        pass
    base = 0 if snap == CKPT_V0 else 2
    seen = base
    if LOG in disk:
        for line in disk[LOG].splitlines():
            line = line.strip()
            if not line:
                continue
            # checkpoint 之前的事件已经固化进快照, 日志里留着也只跳过
            ident = int(line.split("-")[1])
            if ident <= base:
                continue
            if line == "evt-%d" % (seen + 1):
                seen += 1
            else:
                violations.append(Violation(
                    tag=TAG_LOST,
                    message="日志序列断裂: 已到 evt-%d 却遇到 %r, 中间有确认事件丢失"
                            % (seen, line),
                    detail={"expected": "evt-%d" % (seen + 1), "got": line}))
                break
    return "state@%d" % seen, violations


class LogCheckpointBuggy(App):
    name = "logckpt_buggy"
    title = "追加日志+检查点(日志与检查点都不同步) —— 有缺陷"
    pattern = "追加日志+检查点"
    description = (
        "事件 append 后不 fsync 就 ack; checkpoint 直接覆盖写且不 fsync。"
        "断电可导致已确认事件丢失, 或截断的空 checkpoint 让服务拒绝启动。"
    )
    known_defects = [
        {"tag": TAG_LOST,
         "summary": "日志 append 后未 fsync 即确认, 断电丢失已确认写入"},
        {"tag": TAG_CKPT,
         "summary": "checkpoint 覆盖写无 fsync, 截断先于内容落盘 -> 空快照, 恢复失败"},
    ]

    def initial_state(self) -> Dict[str, str]:
        return {LOG: "", CKPT: CKPT_V0}

    def run(self, fs: FsRecorder) -> None:
        fs.append(LOG, E1 + "\n")
        fs.ack("evt-1")                      # 没 fsync 就确认
        fs.append(LOG, E2 + "\n")
        fs.ack("evt-2")
        # 周期性 checkpoint: 直接覆盖
        fs.truncate(CKPT, 0)
        fs.write(CKPT, CKPT_V1, 0)
        fs.append(LOG, E3 + "\n")
        fs.ack("evt-3")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        state, vios = _replay(disk)
        return {"__state__": state,
                "__violations__": "|".join(v.tag for v in vios)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        state = recovered.get("__state__", "MISSING")
        embedded = recovered.get("__violations__", "")
        # 崩溃时刻已经确认了多少条, 恢复就至少要覆盖多少条 —— 确认写入不丢
        required = len(acked)
        if state == "UNRECOVERABLE":
            violations.append(Violation(
                tag=TAG_CKPT,
                message="恢复逻辑拒绝启动: checkpoint 损坏且无可用快照"))
        elif not state.startswith("state@"):
            violations.append(Violation(
                tag=TAG_CKPT, message="恢复出未知状态 %r" % state))
        elif int(state.split("@")[1]) < required:
            violations.append(Violation(
                tag=TAG_LOST,
                message="已确认事件丢失: 已 ack %d 条, 恢复只到 %s"
                        % (required, state),
                detail={"recovered": state, "required": "state@%d" % required}))
        if TAG_CKPT in embedded and not any(v.tag == TAG_CKPT for v in violations):
            violations.append(Violation(tag=TAG_CKPT, message="checkpoint 损坏"))
        return CheckResult(not violations, violations)


class LogCheckpointCorrect(App):
    name = "logckpt_correct"
    title = "追加日志+检查点(append 同步 + 原子检查点) —— 正确写法"
    pattern = "追加日志+检查点"
    description = (
        "每条事件 append 后 fsync 日志再 ack; checkpoint 走 临时文件->fsync->"
        "rename->fsyncdir 的完整协议。崩溃后要么恢复到全部已确认事件, "
        "要么停在确认前的旧状态, 永不出现空快照。"
    )
    known_defects = []

    def initial_state(self) -> Dict[str, str]:
        return {LOG: "", CKPT: CKPT_V0}

    def run(self, fs: FsRecorder) -> None:
        fs.append(LOG, E1 + "\n")
        fs.fsync(LOG)
        fs.ack("evt-1")
        fs.append(LOG, E2 + "\n")
        fs.fsync(LOG)
        fs.ack("evt-2")
        fs.create(TMP)
        fs.write(TMP, CKPT_V1, 0)
        fs.fsync(TMP)
        fs.rename(TMP, CKPT)
        fs.fsyncdir("/")
        fs.append(LOG, E3 + "\n")
        fs.fsync(LOG)
        fs.ack("evt-3")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        state, vios = _replay(disk)
        return {"__state__": state,
                "__violations__": "|".join(v.tag for v in vios)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        state = recovered.get("__state__", "MISSING")
        required = len(acked)
        if state == "UNRECOVERABLE" or not state.startswith("state@"):
            violations.append(Violation(
                tag=TAG_CKPT, message="恢复失败或状态非法: %r" % state))
        elif int(state.split("@")[1]) < required:
            # 正确写法下每个 ack 前都有 fsync, 这种情况本应不可能发生
            violations.append(Violation(
                tag=TAG_LOST,
                message="已确认事件丢失: 已 ack %d 条, 恢复只到 %s"
                        % (required, state),
                detail={"recovered": state, "required": "state@%d" % required}))
        return CheckResult(not violations, violations)
