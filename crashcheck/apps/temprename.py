"""模式二: 临时文件 + 原子重命名(题目中的事故原型)。

"先写临时文件再 rename" 常被误以为天然安全。实际上 rename 的原子性只保证
"目录项替换"这一个动作本身不会出现半截状态, 它并不保证被改名文件的数据
已经落盘。数据面(临时文件的页)与目录面(rename 目录项)是两个独立平面:

  事故 A: rename 落盘了, 但临时文件的内容还在页缓存里 -> 新名字指向空文件,
          旧文件已被原子替换, 服务重启后读到空状态起不来。
  事故 B(本模式另埋一个缺陷): 连目录都没 fsync, rename 本身也可能整体丢失,
          新文件整个不见(题目里"新建的文件整个不见了")。

正确顺序: 写临时文件 -> fsync(临时文件) -> rename -> fsync(父目录)。
"""
from __future__ import annotations

from typing import Dict, List

from ..model import FsRecorder
from .base import App, CheckResult, Violation

DATA = "/config.yaml"
TMP = "/config.yaml.tmp"
V0 = "name: old\nversion: 1\n"
V1 = "name: new\nversion: 2\n"
TAG_EMPTY = "rename-empty-replacement"
TAG_MISSING = "rename-dir-not-fsynced"


class TempRenameBuggy(App):
    name = "temprename_buggy"
    title = "临时文件+重命名(两处同步都缺) —— 有缺陷"
    pattern = "临时文件+重命名"
    description = (
        "写临时文件后直接 rename 替换正式文件, 既没有 fsync 临时文件, "
        "也没有 fsync 父目录。rename 可能先于数据落盘产生空文件替换, "
        "rename 自身也可能整体丢失导致新文件不见。"
    )
    known_defects = [
        {"tag": TAG_EMPTY,
         "summary": "rename 前未 fsync 临时文件, 重命名先于数据落盘 -> 空文件替换旧文件"},
        {"tag": TAG_MISSING,
         "summary": "rename 后未 fsync 父目录, 目录项可能整体丢失 -> 新文件不见"},
    ]

    def initial_state(self) -> Dict[str, str]:
        return {DATA: V0}

    def run(self, fs: FsRecorder) -> None:
        fs.create(TMP)
        fs.write(TMP, V1, 0)
        fs.rename(TMP, DATA)        # 原子替换, 但两个平面都没同步
        fs.ack("config-updated")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        # 应用启动时只认正式路径; 残留临时文件不参与恢复
        return {p: c for p, c in disk.items() if p == DATA or p == TMP}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        if DATA not in recovered:
            # 新文件 create 的目录项未随目录同步, 整个不见
            violations.append(Violation(
                tag=TAG_MISSING,
                message="正式配置 %s 整个丢失: rename 目录项未随目录同步落盘" % DATA,
                detail={"path": DATA}))
            return CheckResult(False, violations)
        content = recovered[DATA]
        if content not in (V0, V1):
            violations.append(Violation(
                tag=TAG_EMPTY,
                message="%s 内容为 %r: rename 已生效但新数据未落盘, 旧版本已被替换"
                        % (DATA, content),
                detail={"path": DATA, "content": content}))
        elif acked and content == V0:
            # 已确认更新却仍是旧版本: rename 目录项整体丢失, 新文件从未出现在盘上
            violations.append(Violation(
                tag=TAG_MISSING,
                message="更新已确认但 %s 仍是旧版本: rename 目录项未随目录同步,"
                        "新文件整个不见" % DATA,
                detail={"path": DATA}))
        return CheckResult(not violations, violations)


class TempRenamePartial(App):
    name = "temprename_partial"
    title = "临时文件+重命名(只 fsync 数据, 忘了目录) —— 有缺陷"
    pattern = "临时文件+重命名"
    description = (
        "开发者记住了 fsync 临时文件, 却以为 rename 原子就够了、没同步父目录。"
        "空文件替换被消除, 但断电仍可能让整个 rename 丢失, 新文件不见。"
    )
    known_defects = [
        {"tag": TAG_MISSING,
         "summary": "rename 后未 fsync 父目录, 目录项可能整体丢失 -> 新文件不见"},
    ]

    def initial_state(self) -> Dict[str, str]:
        return {DATA: V0}

    def run(self, fs: FsRecorder) -> None:
        fs.create(TMP)
        fs.write(TMP, V1, 0)
        fs.fsync(TMP)              # 数据安全了
        fs.rename(TMP, DATA)       # 目录项没人管
        fs.ack("config-updated")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        return {p: c for p, c in disk.items() if p in (DATA, TMP)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        if DATA not in recovered:
            violations.append(Violation(
                tag=TAG_MISSING,
                message="正式配置 %s 整个丢失: 目录项变更未 fsync 目录" % DATA,
                detail={"path": DATA}))
        elif recovered[DATA] not in (V0, V1):
            violations.append(Violation(
                tag=TAG_EMPTY,
                message="%s 出现非法内容 %r" % (DATA, recovered[DATA]),
                detail={"path": DATA, "content": recovered[DATA]}))
        elif acked and recovered[DATA] == V0:
            violations.append(Violation(
                tag=TAG_MISSING,
                message="更新已确认但仍为旧版本: rename 目录项未随目录同步落盘",
                detail={"path": DATA}))
        return CheckResult(not violations, violations)


class TempRenameCorrect(App):
    name = "temprename_correct"
    title = "临时文件+重命名(数据+目录双 fsync) —— 正确写法"
    pattern = "临时文件+重命名"
    description = (
        "教科书式原子替换: 写临时文件 -> fsync 临时文件(数据+inode) -> "
        "rename -> fsync 父目录(目录项)。崩溃后正式文件只可能是旧版或新版。"
    )
    known_defects = []

    def initial_state(self) -> Dict[str, str]:
        return {DATA: V0}

    def run(self, fs: FsRecorder) -> None:
        fs.create(TMP)
        fs.write(TMP, V1, 0)
        fs.fsync(TMP)
        fs.rename(TMP, DATA)
        fs.fsyncdir("/")
        fs.ack("config-updated")

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        return {p: c for p, c in disk.items() if p in (DATA, TMP)}

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        violations: List[Violation] = []
        if DATA not in recovered:
            violations.append(Violation(
                tag=TAG_MISSING, message="%s 丢失" % DATA, detail={"path": DATA}))
        elif recovered[DATA] not in (V0, V1):
            violations.append(Violation(
                tag=TAG_EMPTY, message="%s 非法内容 %r" % (DATA, recovered[DATA]),
                detail={"path": DATA, "content": recovered[DATA]}))
        elif acked and recovered[DATA] == V0:
            violations.append(Violation(
                tag=TAG_MISSING, message="已确认更新但仍为旧版本(目录项未持久化)",
                detail={"path": DATA}))
        return CheckResult(not violations, violations)
