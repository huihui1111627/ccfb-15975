"""带页缓存的文件系统仿真与持久化规则模型。

真实机器上一次写操作的旅程(简化但保留与崩溃一致性相关的全部语义):

    应用 write()  ->  内核页缓存(对后续读立刻可见)  ->  磁盘(掉电才不丢)
    应用 rename() ->  目录项缓存                    ->  磁盘上的目录块
    fsync(fd)     ->  把该文件此前的脏页 + inode 元数据强制推到磁盘
    fsync(dirfd)  ->  把该目录此前的目录项变更(create/rename/delete)推到磁盘

关键事实(本仿真严格复刻):
  1. 没有同步调用时, 页缓存中的数据按磁盘调度顺序落盘, 应用看到的顺序与
     落盘顺序无关 —— 任意重排、甚至完全丢失都可能在掉电时发生。
  2. fsync(文件) 与 fsync(目录) 管的是两个不同的平面:
       数据面  write/append/truncate 由文件 fsync 保证
       目录面  create/rename/delete  由父目录 fsync 保证
     一个 rename 落盘了, 被改名文件的数据页可能还没落盘; 反之亦然。
  3. 一次已完成的同步调用构成屏障: 该调用之前的同平面操作保证落盘,
     之后的操作之间不保证任何顺序。
  4. 一次 write/append 调用自身是原子的(要么整块在, 要么整块不在),
     截断与写之间没有任何原子性保证 —— 这正是"覆盖写掉电变空文件"的来源。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

ROOT = "/"

# 操作类型
CREATE = "CREATE"          # 新建文件(目录面)
WRITE = "WRITE"            # 指定偏移写(数据面)
APPEND = "APPEND"          # 追加写(数据面)
TRUNCATE = "TRUNCATE"      # 截断/扩空洞(数据面, 同时改 inode 长度)
RENAME = "RENAME"          # 重命名/替换(目录面)
DELETE = "DELETE"          # 删除目录项(目录面)
FSYNC = "FSYNC"            # 同步文件(数据面屏障)
FSYNCDIR = "FSYNCDIR"      # 同步目录(目录面屏障)
ACK = "ACK"                # 应用层"已向用户确认"标记(不落盘, 供校验)

DATA_OPS = (WRITE, APPEND, TRUNCATE)
DIRENT_OPS = (CREATE, RENAME, DELETE)
SYNC_OPS = (FSYNC, FSYNCDIR)


@dataclass
class Op:
    """一次被记录的文件操作。inode 为操作目标的 inode 标识(跨 rename 稳定)。"""
    seq: int
    kind: str
    path: Optional[str] = None          # 主操作路径
    dst: Optional[str] = None           # rename 目标路径
    offset: Optional[int] = None        # write/append 现场偏移快照
    length: Optional[int] = None        # truncate 目标长度
    data: Optional[str] = None          # 写入载荷(字符串模拟字节)
    label: Optional[str] = None         # ACK 标记
    inode: Optional[str] = None         # 数据操作/文件同步对应的 inode id
    dirpath: Optional[str] = None       # 目录操作/目录同步对应的目录

    def short(self) -> str:
        if self.kind == CREATE:
            return "create(%s)" % self.path
        if self.kind == WRITE:
            return "write(%s@%d,%r)" % (self.path, self.offset, self.data)
        if self.kind == APPEND:
            return "append(%s,%r)" % (self.path, self.data)
        if self.kind == TRUNCATE:
            return "truncate(%s->%d)" % (self.path, self.length)
        if self.kind == RENAME:
            return "rename(%s->%s)" % (self.path, self.dst)
        if self.kind == DELETE:
            return "delete(%s)" % self.path
        if self.kind == FSYNC:
            return "fsync(%s)" % self.path
        if self.kind == FSYNCDIR:
            return "fsyncdir(%s)" % self.dirpath
        if self.kind == ACK:
            return "ack(%s)" % self.label
        return repr(self.kind)


def parent_dir(path: str) -> str:
    assert path.startswith("/")
    if path == ROOT:
        return ROOT
    d = path.rsplit("/", 1)[0]
    return d if d else ROOT


class FsRecorder:
    """带页缓存的文件系统仿真, 同时记录应用发出的全部操作。

    对应用而言它就是一个普通的内存文件系统: 读永远命中页缓存, 看到的是
    最新写入; 崩溃时磁盘上有什么由 enumerator 依据本类记录的操作序列决定。
    """

    def __init__(self, preexisting: Optional[Dict[str, str]] = None) -> None:
        self.ops: List[Op] = []
        self._live: Dict[str, bytearray] = {}    # 页缓存: 路径 -> 内容(始终最新)
        self._dirents: Set[str] = set()
        self._inode_of: Dict[str, str] = {}      # 路径 -> inode id
        # 应用运行前已在磁盘上的旧版本基线, 不受本次更新重排影响
        self.preexisting: Dict[str, str] = dict(preexisting or {})
        for path, content in self.preexisting.items():
            self._dirents.add(path)
            self._live[path] = bytearray(content.encode("utf-8"))
            self._inode_of[path] = "base:" + path

    # ------------------------------------------------------------------ 基础
    def _add(self, kind: str, **kw) -> Op:
        op = Op(seq=len(self.ops), kind=kind, **kw)
        self.ops.append(op)
        return op

    def exists(self, path: str) -> bool:
        return path in self._dirents

    def read(self, path: str) -> Optional[str]:
        if path not in self._dirents:
            return None
        return self._live[path].decode("utf-8")

    def paths(self) -> List[str]:
        return sorted(self._dirents)

    # ------------------------------------------------------------------ 操作
    def create(self, path: str) -> None:
        if path in self._dirents:
            raise FileExistsError(path)
        inode = "ino%d" % len(self.ops)
        self._add(CREATE, path=path, dirpath=parent_dir(path), inode=inode)
        self._dirents.add(path)
        self._live[path] = bytearray()
        self._inode_of[path] = inode

    def write(self, path: str, data: str, offset: int = 0) -> None:
        """指定偏移覆盖写。offset 缺省 0 —— 它不自动截断文件。"""
        if path not in self._dirents:
            raise FileNotFoundError(path)
        buf = self._live[path]
        if offset > len(buf):
            buf.extend(b"\0" * (offset - len(buf)))
        payload = data.encode("utf-8")
        buf[offset:offset + len(payload)] = payload
        self._add(WRITE, path=path, offset=offset, data=data,
                  inode=self._inode_of[path])

    def append(self, path: str, data: str) -> int:
        if path not in self._dirents:
            raise FileNotFoundError(path)
        offset = len(self._live[path])
        self._live[path].extend(data.encode("utf-8"))
        self._add(APPEND, path=path, offset=offset, data=data,
                  inode=self._inode_of[path])
        return offset

    def truncate(self, path: str, length: int = 0) -> None:
        if path not in self._dirents:
            raise FileNotFoundError(path)
        buf = self._live[path]
        del buf[length:]
        if length > len(buf):
            buf.extend(b"\0" * (length - len(buf)))
        self._add(TRUNCATE, path=path, length=length,
                  inode=self._inode_of[path])

    def rename(self, src: str, dst: str) -> None:
        if src not in self._dirents:
            raise FileNotFoundError(src)
        self._add(RENAME, path=src, dst=dst, dirpath=parent_dir(src))
        self._dirents.discard(dst)
        self._dirents.remove(src)
        self._dirents.add(dst)
        self._live[dst] = self._live.pop(src)
        self._inode_of[dst] = self._inode_of.pop(src)

    def delete(self, path: str) -> None:
        if path not in self._dirents:
            raise FileNotFoundError(path)
        self._add(DELETE, path=path, dirpath=parent_dir(path))
        self._dirents.discard(path)
        self._live.pop(path, None)
        self._inode_of.pop(path, None)

    def fsync(self, path: str) -> None:
        """文件同步屏障: 该 inode 此前所有数据面操作保证落盘。"""
        self._add(FSYNC, path=path, inode=self._inode_of.get(path))

    def fsyncdir(self, directory: str = ROOT) -> None:
        """目录同步屏障: 该目录此前所有目录面操作保证落盘。"""
        self._add(FSYNCDIR, dirpath=directory)

    def ack(self, label: str) -> None:
        """应用声明: 此前所有更新已经向用户确认, 崩溃后不许丢。"""
        self._add(ACK, label=label)

    # ----------------------------------------------------------- 持久化建模
    def groups(self) -> Dict[Tuple[str, Tuple[str, str]], List[int]]:
        """把操作按 (平面, 对象) 分成独立落盘的组。

        ("data", inode id) —— 该文件的数据面操作序列(跨 rename 稳定)
        ("dir",  目录)     —— 该目录的目录面操作序列
        不同组之间没有任何顺序保证, 这正是重排的来源。
        """
        g: Dict[Tuple[str, Tuple[str, str]], List[int]] = {}
        for op in self.ops:
            if op.kind in DATA_OPS or op.kind == FSYNC:
                if op.inode is None:
                    continue
                key = ("data", op.inode)
            elif op.kind in DIRENT_OPS or op.kind == FSYNCDIR:
                key = ("dir", op.dirpath)
            else:
                continue
            g.setdefault(key, []).append(op.seq)
        return g

    def barrier_before(self, crash_at: int) -> Dict[Tuple[str, str], int]:
        """每个组在崩溃点 crash_at 之前最后一次同步屏障覆盖到的最大序号。

        屏障语义: 一次已完成的 fsync 保证"它之前"该组的全部操作落盘,
        因此屏障覆盖到的序号 = 屏障 op 前一个同组操作的序号;
        屏障之后的操作处于"待落盘"状态, 可任意重排或丢失。
        """
        barriers: Dict[Tuple[str, str], int] = {}
        group_last_seen: Dict[Tuple[str, str], int] = {}
        for op in self.ops[:crash_at]:
            if op.kind in DATA_OPS or op.kind in DIRENT_OPS:
                if op.kind in DATA_OPS:
                    key = ("data", op.inode)
                else:
                    key = ("dir", op.dirpath)
                group_last_seen[key] = op.seq
            elif op.kind == FSYNC and op.inode is not None:
                key = ("data", op.inode)
                barriers[key] = group_last_seen.get(key, -1)
            elif op.kind == FSYNCDIR:
                key = ("dir", op.dirpath)
                barriers[key] = group_last_seen.get(key, -1)
        return barriers

    def replay(self, persisted: Set[int],
               preexisting: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """按给定"已落盘操作集合"回放, 得到崩溃后磁盘上实际可见的文件。

        严格按程序序处理持久化下来的操作(被丢掉的操作跳过)。
        inode 与目录项是两个独立对象: 数据落了盘但目录项没落, 文件不可见;
        目录项落了盘但数据没落, 看到的就是空文件/旧内容文件。

        preexisting: 应用运行前就已经在磁盘上的文件(旧版本基线),
        这些内容不受本次更新重排影响, 始终在盘上。
        """
        inodes: Dict[str, bytearray] = {}   # inode id -> 磁盘内容
        dirents: Dict[str, str] = {}        # 路径 -> inode id
        # 预置(运行前就在盘上)的目录项: 本次会话的 rename 不能以它们为源,
        # 因为对应的 CREATE/rename 操作若没落盘, 磁盘上根本没发生过这次改名
        preexisting_dirents: Set[str] = set()
        if preexisting:
            for p, c in preexisting.items():
                iid = "base:" + p           # 与 FsRecorder._inode_of 的预置键一致
                inodes[iid] = bytearray(c.encode("utf-8"))
                dirents[p] = iid
                preexisting_dirents.add(p)

        for op in self.ops:
            if op.seq not in persisted:
                continue
            kind = op.kind
            if kind == CREATE:
                inodes.setdefault(op.inode, bytearray())
                dirents[op.path] = op.inode
                preexisting_dirents.discard(op.path)
            elif kind in (WRITE, APPEND):
                buf = inodes.setdefault(op.inode, bytearray())
                payload = op.data.encode("utf-8")
                if kind == APPEND:
                    buf.extend(payload)     # 前序 append 若丢失, 偏移自然前移
                else:
                    off = op.offset
                    if off > len(buf):
                        buf.extend(b"\0" * (off - len(buf)))
                    buf[off:off + len(payload)] = payload
            elif kind == TRUNCATE:
                buf = inodes.setdefault(op.inode, bytearray())
                del buf[op.length:]
                if op.length > len(buf):
                    buf.extend(b"\0" * (op.length - len(buf)))
            elif kind == RENAME:
                # 源目录项必须由本次会话已落盘的 CREATE/rename 产生;
                # 若它只是预置旧目录项而源文件的 CREATE 丢了, 磁盘上 rename 无效
                if op.path not in dirents or op.path in preexisting_dirents:
                    continue
                old_id = dirents.pop(op.path)
                preexisting_dirents.discard(op.path)
                if op.dst in dirents:      # 原子替换: 目标 inode 被解链
                    victim = dirents.pop(op.dst)
                    inodes.pop(victim, None)
                dirents[op.dst] = old_id
                preexisting_dirents.discard(op.dst)
                # inode id 跨 rename 稳定, 数据操作始终按 op.inode 寻址, 无需改名
            elif kind == DELETE:
                old_id = dirents.pop(op.path, None)
                preexisting_dirents.discard(op.path)
                if old_id is not None:
                    inodes.pop(old_id, None)
            # FSYNC / FSYNCDIR / ACK 回放时无效果
        return {p: inodes[i].decode("utf-8")
                for p, i in sorted(dirents.items()) if i in inodes}

    def signature(self, persisted: Set[int]) -> str:
        return json.dumps(self.replay(persisted, self.preexisting),
                          sort_keys=True, ensure_ascii=False)
