"""崩溃一致性检测主流程。

对枚举器产生的每个崩溃后磁盘状态:
  1. 调用被测应用的 recover() 运行它自己的恢复逻辑;
  2. 调用 check() 校验应用自己声明的一致性条件
     (数据只能是旧版或新版、已确认写入不丢);
  3. 对失败状态做 delta-debug 归约, 找到最小的"丢失操作集合";
  4. 依据丢失/保留操作的模式定位缺失的同步调用, 给出修复建议。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .apps.base import App, Violation
from .enumerator import CrashState, Enumerator
from .model import (
    ACK, APPEND, DIRENT_OPS, FSYNC, FSYNCDIR, RENAME, TRUNCATE, WRITE,
    FsRecorder,
)


@dataclass
class Failure:
    crash_at: int
    violations: List[Violation]
    state: CrashState
    minimal_dropped: List[int]          # 归约后的最小丢失操作序号
    minimal_replay: List[int]           # 最小场景实际保留的操作序号
    advice: List[str]                   # 修复建议(人读)
    missing_syncs: List[str]            # 缺失同步调用的定位描述
    disk_view: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "crash_at": self.crash_at,
            "tags": [v.tag for v in self.violations],
            "violations": [{"tag": v.tag, "message": v.message,
                            "detail": v.detail} for v in self.violations],
            "source": self.state.source,
            "danger_tags": self.state.danger_tags,
            "minimal_dropped": self.minimal_dropped,
            "minimal_replay": self.minimal_replay,
            "advice": self.advice,
            "missing_syncs": self.missing_syncs,
            "disk_view": self.disk_view,
        }


@dataclass
class AppReport:
    app_name: str
    title: str
    pattern: str
    description: str
    operations: List[Dict[str, Any]]
    failures: List[Failure]
    states_explored: int
    elapsed_ms: float
    skipped_by_budget: int
    sources: Dict[str, int]
    known_defects: List[Dict[str, str]]
    naive_states: int = 0
    naive_failures: List[Failure] = field(default_factory=list)
    naive_elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_name": self.app_name,
            "title": self.title,
            "pattern": self.pattern,
            "description": self.description,
            "operations": self.operations,
            "states_explored": self.states_explored,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "skipped_by_budget": self.skipped_by_budget,
            "sources": self.sources,
            "known_defects": self.known_defects,
            "failures": [f.to_dict() for f in self.failures],
            "naive_states": self.naive_states,
            "naive_elapsed_ms": round(self.naive_elapsed_ms, 2),
            "naive_failures": [f.to_dict() for f in self.naive_failures],
        }


class CrashChecker:
    def __init__(self, budget: int = 128, seed: int = 20261124) -> None:
        self.budget = budget
        self.seed = seed

    # ------------------------------------------------------------ 单次校验
    def _run_recovery(self, app: App, fs: FsRecorder, initial: Dict[str, str],
                      state: CrashState) -> List[Violation]:
        disk = state.disk_view(fs)
        # 崩溃点之前的 ack 才算数(ack 操作本身不落盘, 但崩溃时它已发出)
        acked = [op.label for op in fs.ops[:state.crash_at] if op.kind == ACK]
        recovered = app.recover(disk)
        result = app.check(initial, recovered, acked)
        return result.violations

    # ------------------------------------------------------------ 最小化
    def _minimize(self, app: App, fs: FsRecorder, initial: Dict[str, str],
                  state: CrashState, violations: List[Violation]
                  ) -> tuple:
        """delta-debug: 在保持同样违规标签可复现的前提下, 尽量把丢失操作
        集合缩小(等价地, 尽量把待落盘操作加回来), 得到最小问题重排。"""
        want_tags = {v.tag for v in violations}
        # 当前失败场景 = 保证落盘 ∪ (待落盘 - 已丢失)
        pending_all = set(state.dropped) | (
            state.persisted - self._guaranteed_at(fs, state.crash_at))
        dropped = set(state.dropped)

        def still_fails(drop_set: Set[int]) -> bool:
            persisted = (self._guaranteed_at(fs, state.crash_at)
                         | (pending_all - drop_set))
            sig = fs.signature(persisted)
            st = CrashState(crash_at=state.crash_at, persisted=persisted,
                            dropped=sorted(drop_set), signature=sig)
            vios = self._run_recovery(app, fs, initial, st)
            return want_tags <= {v.tag for v in vios}

        if not dropped:
            # 原始状态是"零丢失"的程序序中间态(如截断后、写入前)。它说明协议
            # 在该崩溃点本身就不成立, 但用户要的是"出问题的重排": 尝试把
            # 待落盘操作逐个丢掉, 找到能复现违规的最小非空丢失集合。
            for seq in sorted(pending_all, reverse=True):
                if still_fails({seq}):
                    dropped = {seq}
                    break
            if not dropped:
                dropped = set(pending_all)   # 兜底: 全丢必然复现
        # 贪心: 逐个尝试把丢失操作"加回来", 只要失败仍可复现就缩小集合
        for seq in sorted(dropped):
            trial = dropped - {seq}
            if trial and still_fails(trial):
                dropped = trial
        final_persisted = (self._guaranteed_at(fs, state.crash_at)
                           | (pending_all - dropped))
        return sorted(dropped), sorted(final_persisted)

    @staticmethod
    def _guaranteed_at(fs: FsRecorder, crash_at: int) -> Set[int]:
        barriers = fs.barrier_before(crash_at)
        guaranteed: Set[int] = set()
        for key, members in fs.groups().items():
            b = barriers.get(key, -1)
            guaranteed.update(s for s in members
                              if s < crash_at and s <= b)
        return guaranteed

    # ------------------------------------------------------------ 修复建议
    @staticmethod
    def _advise(fs: FsRecorder, dropped: List[int],
                persisted: Set[int]) -> tuple:
        ops = fs.ops
        by_seq = {op.seq: op for op in ops}
        advice: List[str] = []
        missing: List[str] = []
        dropped_kinds = {by_seq[s].kind for s in dropped}
        dropped_paths = sorted({by_seq[s].path for s in dropped
                                if by_seq[s].path})

        has_data_loss = bool(dropped_kinds & {WRITE, APPEND, TRUNCATE})
        has_dirent_kept = any(by_seq[s].kind in DIRENT_OPS
                              for s in persisted if s in by_seq)
        has_dirent_loss = bool(dropped_kinds & set(DIRENT_OPS))
        has_append_loss = APPEND in dropped_kinds
        has_rename = any(op.kind == RENAME for op in ops)
        has_rename_kept = any(by_seq[s].kind == RENAME
                              for s in persisted if s in by_seq)

        fsync_seqs = [op.seq for op in ops if op.kind == FSYNC]
        where = "、".join(dropped_paths) if dropped_paths else "被更新文件"

        # ---- 场景 1: 重命名已生效但数据丢失(题目中的事故原型) ----
        if has_rename_kept and has_data_loss:
            r = next(by_seq[s] for s in persisted
                     if s in by_seq and by_seq[s].kind == RENAME)
            missing.append("rename(%s -> %s) 之前缺少对源文件 %s 的 fsync"
                           % (r.path, r.dst, r.path))
            advice.append(
                "原子替换必须按固定顺序: 写临时文件 -> fsync(临时文件 %s) -> "
                "rename(%s, %s) -> fsync(父目录)。rename 只保证目录项替换本身"
                "原子, 不保证被改名文件的数据已落盘; 当前场景下 rename 先于数据"
                "落盘, 空文件替换掉了完好的旧文件。" % (r.path, r.path, r.dst))

        # ---- 场景 2: 追加(日志)已确认却丢失 ----
        if has_append_loss and not has_rename:
            if not fsync_seqs:
                missing.append("追加写 %s 后、ack 之前缺少 fsync" % where)
                advice.append(
                    "日志类文件每次 append 之后、向调用方返回成功之前必须 "
                    "fsync(fd); 否则追加内容可能整段停留在页缓存, 断电后表现为"
                    "已确认的写入整条消失, 且没有任何报错。")
            else:
                missing.append("部分追加文件未在确认前 fsync: %s" % where)
                advice.append(
                    "确认每个 append 的文件都在 ack 前单独 fsync —— "
                    "一次 fsync 只覆盖该文件自己的脏页。")

        # ---- 场景 3: 覆盖写(截断+写)撕裂 ----
        if has_data_loss and not has_rename and not has_append_loss:
            if not fsync_seqs:
                missing.append("覆盖写 %s 缺少 fsync" % where)
                advice.append(
                    "truncate 与 write 是两个独立落盘单位。在写入完成后、确认前"
                    "调用 fsync(fd), 把数据和 inode 长度一起推盘; 更稳妥的做法是"
                    "改用'临时文件 + fsync + rename + fsyncdir'的原子替换, "
                    "彻底消除截断后崩溃的空文件窗口。")
            else:
                missing.append("部分数据文件未在后续操作前 fsync: %s" % where)
                advice.append(
                    "扩大 fsync 覆盖范围: 每个被写的文件(含临时文件)都要在 "
                    "rename/ack 之前单独 fsync。")

        # ---- 场景 4: 目录项(create/rename/delete)未同步 ----
        if has_dirent_loss or (has_rename and has_dirent_kept and
                               not any(op.kind == FSYNCDIR for op in ops)):
            missing.append("目录项变更后缺少 fsync(父目录)")
            advice.append(
                "create/rename/delete 之后必须打开父目录并 fsync 该目录 fd"
                "(POSIX: dirfd=open(dir, O_RDONLY); fsync(dirfd)); "
                "不同步目录, 断电时目录项变更整体回滚, 表现为新建文件整个不见、"
                "rename 像从未发生。")

        if not advice:
            advice.append("核对同步屏障覆盖范围: 只有同步调用之前的同平面操作才"
                          "保证落盘, 屏障之后的新操作需要再次同步。")
        # 去重保序
        seen = set()
        uniq_advice = [a for a in advice if not (a in seen or seen.add(a))]
        return uniq_advice, sorted(set(missing))

    # ------------------------------------------------------------ 主入口
    def check_app(self, app: App, naive: bool = False) -> AppReport:
        import time
        t0 = time.perf_counter()
        initial = app.initial_state()
        # 预置初始文件 = 运行前已在磁盘上的旧版本, 不产生操作、不参与重排
        fs = FsRecorder(preexisting=initial)
        app.run(fs)

        if naive:
            enum = Enumerator(fs, budget=self.budget, seed=self.seed)
            states = enum.enumerate_naive()
        else:
            enum = Enumerator(fs, budget=self.budget, seed=self.seed)
            states = enum.enumerate_all()

        failures: List[Failure] = []
        seen_failure_tags: Set[tuple] = set()
        sources: Dict[str, int] = {}
        for st in states:
            sources[st.source] = sources.get(st.source, 0) + 1
            acked = [op.label for op in fs.ops[:st.crash_at]
                     if op.kind == ACK]
            # 应用还没向任何人确认过更新: 任何中间崩溃状态都不算违规,
            # 只校验 ack 之后"确认写入不丢"的承诺
            if not acked:
                continue
            vios = self._run_recovery(app, fs, initial, st)
            if not vios:
                continue
            tag_key = tuple(sorted(v.tag for v in vios))
            minimal_drop, minimal_keep = self._minimize(
                app, fs, initial, st, vios)
            advice, missing = self._advise(fs, set(minimal_drop),
                                           set(minimal_keep))
            failure = Failure(
                crash_at=st.crash_at, violations=vios, state=st,
                minimal_dropped=minimal_drop, minimal_replay=minimal_keep,
                advice=advice, missing_syncs=missing,
                disk_view=fs.replay(set(minimal_keep), fs.preexisting))
            failures.append(failure)
            seen_failure_tags.add(tag_key)

        # 同一缺陷类别只保留一个最小代表(序号集合最小者), 让报告聚焦
        best: Dict[str, Failure] = {}
        for f in failures:
            for tag in {v.tag for v in f.violations}:
                if tag not in best or len(f.minimal_dropped) < \
                        len(best[tag].minimal_dropped):
                    best[tag] = f
        deduped = sorted(best.values(),
                         key=lambda f: (f.crash_at, len(f.minimal_dropped)))
        elapsed = (time.perf_counter() - t0) * 1000

        op_dump = [{"seq": op.seq, "kind": op.kind, "text": op.short(),
                    "path": op.path, "dir": op.dirpath,
                    "is_sync": op.kind in (FSYNC, FSYNCDIR),
                    "is_ack": op.kind == ACK}
                   for op in fs.ops]
        return AppReport(
            app_name=app.name, title=app.title, pattern=app.pattern,
            description=app.description, operations=op_dump,
            failures=deduped, states_explored=len(states),
            elapsed_ms=elapsed, skipped_by_budget=enum.skipped_by_budget,
            sources=sources, known_defects=app.known_defects)
