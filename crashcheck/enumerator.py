"""崩溃后磁盘可能状态的枚举。

持久化规则(见 model.py 的模块注释)在枚举上的形式化:

对崩溃点 t(第 t 条操作执行完之后断电), 每个落盘组 g:
  * 序号 <= 屏障 barrier_g(t) 的操作: 保证落盘, 必选;
  * 屏障之后的"待落盘"操作 P_g: 每个操作独立地可能落盘 / 丢失,
    组内及组间均无顺序保证 —— 这就是重排与部分落盘的来源。

因此一个崩溃状态 = 全部必选操作 ∪ 各组待落盘操作的一个子集。
若某个组没有同步屏障, 它的全部历史操作都在"待落盘"集合里, 可整组丢失。

朴素方法(对照组): 不考虑重排, 只在操作边界处断电, 磁盘状态是
"全保留"或"只保留到最后一个同步屏障"这两种前缀状态。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .model import (
    APPEND, CREATE, DATA_OPS, DELETE, DIRENT_OPS,
    RENAME, TRUNCATE, WRITE, FsRecorder, Op,
)


@dataclass
class CrashState:
    """一次具体的"断电后世界": 在 crash_at 之后断电, persisted 中的操作落了盘。"""
    crash_at: int
    persisted: Set[int]
    dropped: List[int]
    signature: str
    # 这个状态是通过哪条路径枚举出来的(强制/全枚举/偏向抽样...)
    source: str = ""
    # 危险重排标签, 例如 ("rename-before-data", ...)
    danger_tags: List[str] = field(default_factory=list)

    def disk_view(self, fs: FsRecorder) -> Dict[str, str]:
        return fs.replay(self.persisted, fs.preexisting)


class Enumerator:
    def __init__(self, fs: FsRecorder, budget: int = 128, seed: int = 20261124,
                 dangerous_first: bool = True) -> None:
        self.fs = fs
        self.budget = budget                  # 每个崩溃点最多生成多少个状态
        self.seed = seed
        self.dangerous_first = dangerous_first
        self.states: List[CrashState] = []
        # 每个崩溃点内部按磁盘签名去重(不同崩溃点的 ack 集合不同, 不能跨点去重)
        self._seen: Dict[int, Set[str]] = {}
        # 全局签名统计(仅供报告分析有多少种不同磁盘布局)
        self.all_signatures: Set[str] = set()
        self.skipped_by_budget = 0

    # ------------------------------------------------------------- 组与屏障
    def _pending_groups(self, crash_at: int
                        ) -> Tuple[Dict[Tuple[str, str], List[int]], Set[int],
                                   Dict[Tuple[str, str], int]]:
        groups = self.fs.groups()
        barriers = self.fs.barrier_before(crash_at)
        guaranteed: Set[int] = set()
        pending: Dict[Tuple[str, str], List[int]] = {}
        for key, members in groups.items():
            b = barriers.get(key, -1)
            keep, wait = [], []
            for seq in members:
                if seq >= crash_at:
                    continue
                (keep if seq <= b else wait).append(seq)
            guaranteed.update(keep)
            if wait:
                pending[key] = wait
        return pending, guaranteed, barriers

    # ------------------------------------------------------------- 危险重排
    def _danger_masks(self, pending: Dict[Tuple[str, str], List[int]]
                      ) -> List[Tuple[Set[int], List[str]]]:
        """针对本崩溃点的待落盘操作, 构造"最容易出问题的重排"必测组合。

        经典危险模式:
          D1 rename/目录项 落盘, 但被改名文件的数据丢失  -> 空文件替换了旧文件
          D2 create 目录项落盘, 文件数据丢失              -> 空文件/丢数据
          D3 截断落盘, 后续写入丢失                       -> 覆盖写变空/变短
          D4 已 ACK 的写入丢失(数据全丢)                  -> 确认写入丢失
          D5 数据落盘但目录项丢失(rename/create 丢)       -> 新文件整个不见
        """
        ops_by_seq: Dict[int, Op] = {op.seq: op for op in self.fs.ops}
        all_pending = [s for members in pending.values() for s in members]
        pend_set = set(all_pending)
        masks: List[Tuple[Set[int], List[str]]] = []

        def add(extra_drop: Set[int], tag: str) -> None:
            masks.append((set(all_pending) - extra_drop, [tag]))

        data_seqs = [s for s in all_pending if ops_by_seq[s].kind in DATA_OPS]
        dirent_seqs = [s for s in all_pending if ops_by_seq[s].kind in DIRENT_OPS]

        # D1: rename 保留, 同文件/替换目标的数据丢弃
        renames = [s for s in dirent_seqs if ops_by_seq[s].kind == RENAME]
        for rn in renames:
            op = ops_by_seq[rn]
            victim_data = {s for s in data_seqs
                           if ops_by_seq[s].path in (op.path, op.dst)}
            kept = set(all_pending) - victim_data
            masks.append((kept, ["D1-rename-before-data"]))

        # D2: create 保留, 新 inode 的数据丢弃
        creates = [s for s in dirent_seqs if ops_by_seq[s].kind == CREATE]
        for cr in creates:
            op = ops_by_seq[cr]
            victim_data = {s for s in data_seqs if ops_by_seq[s].path == op.path}
            if victim_data:
                kept = set(all_pending) - victim_data
                masks.append((kept, ["D2-dirent-before-data"]))

        # D3: truncate 保留, 它之后的写丢弃
        trunc = [s for s in data_seqs if ops_by_seq[s].kind == TRUNCATE]
        for tr in trunc:
            op = ops_by_seq[tr]
            later_writes = {s for s in data_seqs
                            if ops_by_seq[s].path == op.path and s > tr}
            if later_writes:
                kept = set(all_pending) - later_writes
                masks.append((kept, ["D3-trunc-before-write"]))

        # D4: 所有数据丢弃, 目录项保留 —— 已确认数据丢失的极端形态
        if data_seqs and dirent_seqs:
            kept = set(all_pending) - set(data_seqs)
            masks.append((kept, ["D4-all-data-lost"]))

        # D5: 数据保留, 目录项丢弃 —— 文件整个不见
        if data_seqs and dirent_seqs:
            kept = set(all_pending) - set(dirent_seqs)
            masks.append((kept, ["D5-data-before-dirent"]))
        return masks

    # ------------------------------------------------------------- 单个崩溃点
    def enumerate_at(self, crash_at: int) -> List[CrashState]:
        pending, guaranteed, _ = self._pending_groups(crash_at)
        all_pending = sorted(s for ms in pending.values() for s in ms)

        # 待落盘子集的生成方式: (persisted 中属于 pending 的部分, 来源, 标签)
        candidates: List[Tuple[Set[int], str, List[str]]] = []
        candidates.append((set(all_pending), "all-durable", []))       # 全部落盘
        candidates.append((set(), "all-lost", []))                     # 全部丢失

        if not all_pending:
            chosen = candidates
        else:
            # 危险重排优先
            if self.dangerous_first:
                for kept, tags in self._danger_masks(pending):
                    candidates.append((kept, "danger", tags))
            total_combos = 1
            for members in pending.values():
                total_combos *= (2 ** len(members))

            if total_combos <= self.budget:
                # 小空间: 完整枚举所有子集(组间笛卡尔积, 覆盖全部重排)
                subsets: List[Set[int]] = [set()]
                for members in pending.values():
                    n = len(members)
                    new = []
                    for base in subsets:
                        for idx in range(2 ** n):
                            new.append(base | {members[j] for j in range(n)
                                               if (idx >> j) & 1})
                    subsets = new
                for sub in subsets:
                    candidates.append((sub, "exhaustive", []))
            else:
                # 状态爆炸: 剪枝 + 偏向抽样
                rng = random.Random(self.seed + crash_at)
                ops_by_seq = {op.seq: op for op in self.fs.ops}
                seen_local: Set[Tuple[int, ...]] = set()
                for kept, _, _ in candidates:
                    seen_local.add(tuple(sorted(kept)))
                while len(candidates) < self.budget:
                    chosen_set: Set[int] = set()
                    for key, members in pending.items():
                        plane = key[0]
                        for s in members:
                            kind = ops_by_seq[s].kind
                            # 偏向: 目录项更"容易"先落盘, 数据更"容易"丢失,
                            # 让抽样集中在数据丢失/目录项保留这类危险区域
                            if kind in DIRENT_OPS:
                                p = 0.8
                            elif kind == TRUNCATE:
                                p = 0.7
                            else:
                                p = 0.4
                            if rng.random() < p:
                                chosen_set.add(s)
                    key = tuple(sorted(chosen_set))
                    if key in seen_local:
                        continue
                    seen_local.add(key)
                    candidates.append((chosen_set, "sampled", []))
                self.skipped_by_budget += total_combos - self.budget
            chosen = candidates

        out: List[CrashState] = []
        local_seen = self._seen.setdefault(crash_at, set())
        for kept_pending, source, tags in chosen:
            persisted = guaranteed | kept_pending
            sig = self.fs.signature(persisted)
            if sig in local_seen:
                continue                       # 同崩溃点内: 相同磁盘状态不重复恢复
            local_seen.add(sig)
            self.all_signatures.add(sig)
            dropped = sorted(set(all_pending) - kept_pending)
            st = CrashState(crash_at=crash_at, persisted=persisted,
                            dropped=dropped, signature=sig,
                            source=source, danger_tags=list(tags))
            self.states.append(st)
            out.append(st)
        return out

    def enumerate_all(self) -> List[CrashState]:
        """在每两个相邻操作之间(及全部执行完之后)各断一次电。"""
        n = len(self.fs.ops)
        for crash_at in range(1, n + 1):
            self.enumerate_at(crash_at)
        return self.states

    # ------------------------------------------------------------- 朴素对照
    def enumerate_naive(self) -> List[CrashState]:
        """朴素方法: 只在操作边界断电, 不考虑重排。

        每个崩溃点只考虑两种前缀:
          a) 已执行操作全部落盘(乐观);
          b) 只保留各同步屏障保证的操作(悲观), 屏障后的待落盘操作全丢。
        """
        n = len(self.fs.ops)
        for crash_at in range(1, n + 1):
            _, guaranteed, _ = self._pending_groups(crash_at)
            executed = {op.seq for op in self.fs.ops[:crash_at]}
            local_seen = self._seen.setdefault(crash_at, set())
            for kept, source in ((executed, "naive-all"),
                                 (guaranteed, "naive-barrier")):
                sig = self.fs.signature(kept)
                if sig in local_seen:
                    continue
                local_seen.add(sig)
                self.all_signatures.add(sig)
                st = CrashState(crash_at=crash_at, persisted=set(kept),
                                dropped=sorted(executed - kept),
                                signature=sig, source=source)
                self.states.append(st)
        return self.states
