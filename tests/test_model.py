"""核心语义单元测试: python3 -I -m unittest tests.test_model -v"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashcheck.model import (
    APPEND, ACK, CREATE, DELETE, FSYNC, FSYNCDIR, RENAME, TRUNCATE, WRITE,
    FsRecorder,
)
from crashcheck.enumerator import Enumerator


class TestReplaySemantics(unittest.TestCase):
    def test_write_atomicity_unit(self):
        """单次 write 是原子单位: 要么整块在, 要么整块不在, 不会出现半截载荷。"""
        fs = FsRecorder(preexisting={"/f": "old"})
        fs.truncate("/f", 0)
        fs.write("/f", "new-data", 0)
        for st in Enumerator(fs).enumerate_all():
            content = st.disk_view(fs)["/f"]
            self.assertIn(content, ("old", "", "new-data"))

    def test_truncate_without_write_yields_empty_file(self):
        """截断落盘、写入丢失 -> 空文件(覆盖写事故的机制本身)。"""
        fs = FsRecorder(preexisting={"/f": "old"})
        fs.truncate("/f", 0)
        fs.write("/f", "new", 0)
        empty_seen = False
        for st in Enumerator(fs).enumerate_all():
            if st.disk_view(fs).get("/f") == "":
                empty_seen = True
        self.assertTrue(empty_seen)

    def test_fsync_is_barrier_for_data_only(self):
        """fsync 后其之前的数据操作保证落盘, 之后的新写仍可丢失。"""
        fs = FsRecorder(preexisting={"/f": "v0"})
        fs.truncate("/f", 0)
        fs.write("/f", "v1", 0)
        fs.fsync("/f")
        fs.write("/f", "v2", 0)          # 屏障之后, 无同步
        fs.ack("done")
        ack_at = next(op.seq for op in fs.ops if op.kind == ACK)
        contents = set()
        for st in Enumerator(fs).enumerate_all():
            if st.crash_at <= ack_at:
                continue                 # 只看确认之后: 旧值不许复活
            contents.add(st.disk_view(fs)["/f"])
        # v1 必须始终可达(屏障保证); v2 可能丢, 丢了就回到 v1
        self.assertIn("v1", contents)
        self.assertNotIn("v0", contents)
        self.assertNotIn("", contents)

    def test_fsyncdir_independent_from_fsync(self):
        """文件 fsync 不保证目录项; 目录 fsync 不保证文件数据。"""
        fs = FsRecorder(preexisting={"/cfg": "old"})
        fs.create("/cfg.tmp")
        fs.write("/cfg.tmp", "new", 0)
        fs.fsync("/cfg.tmp")              # 数据安全
        fs.rename("/cfg.tmp", "/cfg")     # 目录项无人同步
        rename_lost = False
        for st in Enumerator(fs).enumerate_all():
            disk = st.disk_view(fs)
            if disk.get("/cfg") == "old":
                rename_lost = True       # rename 整体丢失, 旧文件原样保留
        self.assertTrue(rename_lost)

    def test_rename_before_data_gives_empty_replacement(self):
        """核心事故: rename 落盘但临时文件数据丢失 -> 空文件替换旧文件。"""
        fs = FsRecorder(preexisting={"/cfg": "old"})
        fs.create("/cfg.tmp")
        fs.write("/cfg.tmp", "new", 0)
        fs.rename("/cfg.tmp", "/cfg")
        found_empty_replacement = False
        for st in Enumerator(fs).enumerate_all():
            disk = st.disk_view(fs)
            if disk.get("/cfg") == "" and "/cfg.tmp" not in disk:
                found_empty_replacement = True
        self.assertTrue(found_empty_replacement)

    def test_create_dirent_lost_makes_file_invisible(self):
        """CREATE 目录项丢失: 数据即使落盘, 文件也不可见(新建文件整个不见)。"""
        fs = FsRecorder()
        fs.create("/new")
        fs.write("/new", "data", 0)
        invisible = False
        for st in Enumerator(fs).enumerate_all():
            if "/new" not in st.disk_view(fs):
                invisible = True
        self.assertTrue(invisible)

    def test_append_offset_shift_on_loss(self):
        """前序 append 丢失时, 后序 append 按磁盘实际长度前移, 不产生空洞错位。"""
        fs = FsRecorder(preexisting={"/log": ""})
        fs.append("/log", "a\n")
        fs.append("/log", "b\n")
        seen = set()
        for st in Enumerator(fs).enumerate_all():
            seen.add(st.disk_view(fs)["/log"])
        self.assertEqual(seen, {"", "a\n", "b\n", "a\nb\n"})

    def test_rename_chain_requires_each_dirent(self):
        """两级 rename: 中间目录项丢了, 第二级 rename 在磁盘上无效。"""
        fs = FsRecorder(preexisting={"/a": "x"})
        fs.create("/t")
        fs.write("/t", "y", 0)
        fs.rename("/t", "/a")
        fs.rename("/a", "/b")
        # 强制只保留第二次 rename、丢掉第一次: 第二次必须无效
        ops = {op.seq for op in fs.ops}
        first_rename = next(op.seq for op in fs.ops if op.kind == RENAME)
        persisted = ops - {first_rename}
        disk = fs.replay(persisted, fs.preexisting)
        self.assertEqual(disk.get("/a"), "x")     # 旧文件未被动过
        self.assertNotIn("/b", disk)

    def test_delete_durability(self):
        """DELETE 未同步时, 已删除文件可能仍在盘上。"""
        fs = FsRecorder(preexisting={"/f": "x"})
        fs.delete("/f")
        still_there = any("/f" in st.disk_view(fs)
                          for st in Enumerator(fs).enumerate_all())
        self.assertTrue(still_there)

    def test_preexisting_never_lost(self):
        """预置基线文件不受本次更新重排影响, 永远不会凭空消失(除非被操作替换)。"""
        fs = FsRecorder(preexisting={"/keep": "base", "/target": "old"})
        fs.truncate("/target", 0)
        for st in Enumerator(fs).enumerate_all():
            self.assertIn("/keep", st.disk_view(fs))


class TestBarrierSemantics(unittest.TestCase):
    def test_barrier_covers_prior_ops_not_self(self):
        fs = FsRecorder(preexisting={"/f": ""})
        fs.append("/f", "a")
        fs.append("/f", "b")
        fs.fsync("/f")
        barriers = fs.barrier_before(3)       # crash 点正好在 fsync 之后
        self.assertEqual(barriers[("data", fs._inode_of["/f"])], 1)

    def test_separate_planes_have_separate_barriers(self):
        fs = FsRecorder(preexisting={"/f": "old"})
        fs.create("/f.tmp")
        fs.write("/f.tmp", "new", 0)
        fs.fsync("/f.tmp")
        fs.rename("/f.tmp", "/f")
        b = fs.barrier_before(len(fs.ops))
        data_key = ("data", fs._inode_of["/f"])
        dir_key = ("dir", "/")
        self.assertIn(data_key, b)            # 数据面有屏障
        self.assertNotIn(dir_key, b)          # 目录面没有任何屏障


class TestEnumeration(unittest.TestCase):
    def test_dedup_per_crash_point(self):
        """同一崩溃点内相同磁盘状态去重, 不同崩溃点即使磁盘相同也各自保留。"""
        fs = FsRecorder(preexisting={"/f": "old"})
        fs.write("/f", "new", 0)
        fs.ack("x")
        enum = Enumerator(fs)
        enum.enumerate_all()
        self.assertGreaterEqual(len(enum._seen), 2)   # 至少两个崩溃点

    def test_budget_caps_explosion(self):
        fs = FsRecorder()
        for i in range(6):
            p = "/g%d" % i
            fs.create(p)
            for j in range(4):
                fs.append(p, "x")
        enum = Enumerator(fs, budget=32)
        states = enum.enumerate_at(len(fs.ops))
        self.assertLessEqual(len(states), 32)
        self.assertGreater(enum.skipped_by_budget, 0)

    def test_danger_masks_present(self):
        fs = FsRecorder(preexisting={"/cfg": "old"})
        fs.create("/cfg.tmp")
        fs.write("/cfg.tmp", "new", 0)
        fs.rename("/cfg.tmp", "/cfg")
        enum = Enumerator(fs)
        enum.enumerate_all()
        tags = {t for st in enum.states for t in st.danger_tags}
        self.assertIn("D1-rename-before-data", tags)

    def test_naive_enumeration_is_prefix_only(self):
        """朴素法每个崩溃点只有全保留/屏障前缀两种形态, 不含跨平面重排。"""
        fs = FsRecorder(preexisting={"/cfg": "old"})
        fs.create("/cfg.tmp")
        fs.write("/cfg.tmp", "new", 0)
        fs.rename("/cfg.tmp", "/cfg")
        enum = Enumerator(fs)
        enum.enumerate_naive()
        for st in enum.states:
            self.assertIn(st.source, ("naive-all", "naive-barrier"))
        # 朴素法永远看不到"空文件替换"这种交叉状态
        for st in enum.states:
            self.assertNotEqual(st.disk_view(fs).get("/cfg"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
