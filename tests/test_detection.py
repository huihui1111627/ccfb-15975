"""检测效果验收测试: 检出率 100%、正确写法零误报、最小化与建议非空。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashcheck.apps import ALL_APPS
from crashcheck.checker import CrashChecker


class TestDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checker = CrashChecker(budget=128)
        cls.reports = {a.name: cls.checker.check_app(a) for a in ALL_APPS}

    def test_all_known_defects_detected(self):
        total_known = total_hit = 0
        for app in ALL_APPS:
            rep = self.reports[app.name]
            known = {d["tag"] for d in app.known_defects}
            found = {v.tag for f in rep.failures for v in f.violations}
            total_known += len(known)
            total_hit += len(known & found)
            self.assertEqual(
                known & found, known,
                msg="%s 漏检 %s" % (app.name, sorted(known - found)))
        self.assertEqual(total_known, 8)
        self.assertEqual(total_hit, 8)

    def test_zero_false_positives_on_correct_apps(self):
        for app in ALL_APPS:
            if not app.known_defects:
                rep = self.reports[app.name]
                self.assertEqual(
                    rep.failures, [],
                    msg="正确写法 %s 出现误报: %s" % (
                        app.name,
                        [v.message for f in rep.failures for v in f.violations]))

    def test_naive_method_misses_cross_plane_reorder_bugs(self):
        """朴素边界法必须漏掉两个跨平面重排缺陷(对照组有效性)。"""
        naive_missed = set()
        for app in ALL_APPS:
            rep = self.checker.check_app(app, naive=True)
            known = {d["tag"] for d in app.known_defects}
            found = {v.tag for f in rep.failures for v in f.violations}
            naive_missed |= (known - found)
        self.assertIn("rename-empty-replacement", naive_missed)
        self.assertIn("wal-commit-not-durable", naive_missed)

    def test_failures_have_minimal_repro_and_advice(self):
        for app in ALL_APPS:
            for f in self.reports[app.name].failures:
                self.assertTrue(f.minimal_dropped)
                self.assertTrue(f.advice)
                self.assertTrue(f.missing_syncs)
                # 最小化结果必须确实可复现同一违规
                tags = {v.tag for v in f.violations}
                self.assertTrue(tags)

    def test_minimal_drop_is_nonempty_and_reproducible(self):
        """每个失败都有非空最小丢失集; 按最小集合回放确实产生违规磁盘状态。"""
        from crashcheck.model import FsRecorder
        from crashcheck.enumerator import CrashState
        for app in ALL_APPS:
            rep = self.reports[app.name]
            initial = app.initial_state()
            for f in rep.failures:
                self.assertTrue(f.minimal_dropped,
                                msg="%s 存在空最小丢失集" % app.name)
                # 用最小保留集独立回放, 必须复现报告里的磁盘视图与违规
                fs = FsRecorder(preexisting=initial)
                app.run(fs)
                disk = fs.replay(set(f.minimal_replay), fs.preexisting)
                self.assertEqual(disk, f.disk_view)
                st = CrashState(crash_at=f.crash_at,
                                persisted=set(f.minimal_replay),
                                dropped=list(f.minimal_dropped),
                                signature="")
                vios = self.checker._run_recovery(app, fs, initial, st)
                self.assertTrue(
                    {v.tag for v in vios} &
                    {v.tag for v in f.violations})


if __name__ == "__main__":
    unittest.main(verbosity=2)
