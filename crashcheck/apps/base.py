"""被测应用基类。

每个被测应用提供三件东西:
  initial_state()  运行前磁盘上已有的文件(用于判断"旧版本")
  run(fs)          一次正常的更新流程, 通过 FsRecorder 记录全部操作,
                   成功向用户确认的更新必须调用 fs.ack(...)
  recover(fs, disk) 崩溃后的恢复逻辑: 只给崩溃后磁盘上真正存在的文件,
                   返回恢复后的逻辑状态
  check(initial, recovered, acked) 应用自己声明的一致性条件
  known_defects    预埋的已知缺陷描述(供验收对照, 检测工具本身不读它)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..model import FsRecorder


@dataclass
class Violation:
    tag: str                       # 机器可比对的缺陷类别
    message: str                   # 人读描述
    detail: Dict[str, str] = field(default_factory=dict)


@dataclass
class CheckResult:
    ok: bool
    violations: List[Violation] = field(default_factory=list)


class App:
    name: str = "base"
    title: str = ""
    description: str = ""
    pattern: str = ""
    known_defects: List[Dict[str, str]] = []

    def initial_state(self) -> Dict[str, str]:
        return {}

    def run(self, fs: FsRecorder) -> None:
        raise NotImplementedError

    def recover(self, disk: Dict[str, str]) -> Dict[str, str]:
        raise NotImplementedError

    def check(self, initial: Dict[str, str], recovered: Dict[str, str],
              acked: List[str]) -> CheckResult:
        raise NotImplementedError

    # 供具体应用复用的小工具 ------------------------------------------------
    @staticmethod
    def require_file(recovered: Dict[str, str], path: str,
                     allowed: List[str], violations: List[Violation],
                     tag: str, what: str) -> None:
        """文件必须存在且内容取自允许的版本集合(旧版/新版), 不许是空/半截。"""
        if path not in recovered:
            violations.append(Violation(
                tag=tag, message="%s: 文件 %s 在崩溃后整个丢失" % (what, path),
                detail={"path": path}))
            return
        content = recovered[path]
        if content not in allowed:
            violations.append(Violation(
                tag=tag,
                message="%s: %s 的内容 %r 既不是旧版本也不是合法新版本"
                        % (what, path, content),
                detail={"path": path, "content": content,
                        "allowed": "|".join(allowed)}))
