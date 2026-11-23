#!/usr/bin/env python3
"""验收驱动: 对全部被测应用运行崩溃一致性检测, 输出验收统计与 HTML 报告。

验收指标(对应需求第 6 条):
  1. 工具发现的缺陷 vs 每个应用预埋的已知缺陷 -> 检出率
  2. 无缺陷正确写法的误报数(必须为 0)
  3. 朴素方法(只在操作边界断电, 不考虑重排)能发现哪些
  4. 每个应用探索的崩溃状态数与耗时

用法:
  python3 -I run_evaluation.py                 # 跑全部应用, 生成 report.html
  python3 -I run_evaluation.py --budget 256    # 调整每崩溃点抽样预算
  python3 -I run_evaluation.py --app wal_buggy # 只跑指定应用
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crashcheck.apps import ALL_APPS, APP_BY_NAME, App
from crashcheck.checker import CrashChecker
from crashcheck.report import render


def evaluate(apps, budget: int = 128, seed: int = 20261124):
    checker = CrashChecker(budget=budget, seed=seed)
    reports = []
    per_app = []
    total_known = total_found = total_missed = 0
    fp_total = 0
    naive_missed_total = 0
    total_states = 0
    t_start = time.perf_counter()

    for app in apps:
        rep = checker.check_app(app)                       # 完整方法(含重排)
        naive_rep = checker.check_app(app, naive=True)     # 朴素对照法
        rep.naive_states = naive_rep.states_explored
        rep.naive_failures = naive_rep.failures
        rep.naive_elapsed_ms = naive_rep.elapsed_ms
        reports.append(rep)

        known_tags = {d["tag"] for d in app.known_defects}
        found_tags = {v.tag for f in rep.failures for v in f.violations}
        naive_tags = {v.tag for f in naive_rep.failures for v in f.violations}
        false_pos = found_tags - known_tags
        missed = known_tags - found_tags
        naive_missed = known_tags - naive_tags

        total_known += len(known_tags)
        total_found += len(known_tags & found_tags)
        total_missed += len(missed)
        fp_total += len(false_pos)
        naive_missed_total += len(naive_missed)
        total_states += rep.states_explored

        per_app.append({
            "name": app.name, "title": app.title, "pattern": app.pattern,
            "known": len(known_tags), "found": len(known_tags & found_tags),
            "missed": len(missed), "false_positives": len(false_pos),
            "known_tags": sorted(known_tags),
            "found_tags": sorted(found_tags),
            "naive_found_tags": sorted(naive_tags),
            "naive_missed_tags": sorted(naive_missed),
            "states": rep.states_explored,
            "naive_states": naive_rep.states_explored,
            "elapsed_ms": round(rep.elapsed_ms, 2),
            "naive_elapsed_ms": round(naive_rep.elapsed_ms, 2),
            "skipped_by_budget": rep.skipped_by_budget,
            "detection_rate": round(100 * len(known_tags & found_tags)
                                    / len(known_tags)) if known_tags else 100,
        })

    total_elapsed = (time.perf_counter() - t_start) * 1000
    summary = {
        "total_known": total_known,
        "total_found": total_found,
        "total_missed": total_missed,
        "detection_rate": round(100 * total_found / total_known) if total_known else 100,
        "false_positives": fp_total,
        "naive_missed": naive_missed_total,
        "total_states": total_states,
        "total_elapsed_ms": round(total_elapsed, 2),
        "budget": budget,
        "per_app": per_app,
    }
    return reports, summary


def print_console(summary):
    print("=" * 96)
    print("应用文件更新崩溃一致性检测 —— 验收结果")
    print("=" * 96)
    hdr = ("%-22s %-5s %-5s %-5s %-5s %-7s %-7s %-9s" %
           ("应用", "预埋", "检出", "漏检", "误报", "状态数", "朴素数", "耗时ms"))
    print(hdr)
    print("-" * 96)
    for r in summary["per_app"]:
        print("%-22s %-5d %-5d %-5d %-5d %-7d %-7d %-9.1f" % (
            r["name"], r["known"], r["found"], r["missed"],
            r["false_positives"], r["states"], r["naive_states"],
            r["elapsed_ms"]))
    print("-" * 96)
    print("预埋缺陷总数: %d   检出: %d   漏检: %d   总检出率: %d%%" % (
        summary["total_known"], summary["total_found"],
        summary["total_missed"], summary["detection_rate"]))
    print("正确写法误报数: %d (要求为 0)" % summary["false_positives"])
    print("朴素边界断电法漏检缺陷数: %d" % summary["naive_missed"])
    for r in summary["per_app"]:
        if r["naive_missed_tags"]:
            print("  - %s 朴素法漏检: %s" %
                  (r["name"], ", ".join(r["naive_missed_tags"])))
    print("总崩溃状态数: %d   总耗时: %.1f ms" %
          (summary["total_states"], summary["total_elapsed_ms"]))
    print("=" * 96)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=128,
                    help="每个崩溃点待落盘子集抽样上限(默认128)")
    ap.add_argument("--seed", type=int, default=20261124)
    ap.add_argument("--app", type=str, default=None,
                    help="只跑指定应用(默认全部)")
    ap.add_argument("--out", type=str,
                    default=os.path.join(os.path.dirname(
                        os.path.abspath(__file__)), "report.html"))
    ap.add_argument("--json", type=str, default=None,
                    help="同时把结构化结果写入该 JSON 文件")
    args = ap.parse_args()

    apps = [APP_BY_NAME[args.app]] if args.app else ALL_APPS
    reports, summary = evaluate(apps, budget=args.budget, seed=args.seed)
    print_console(summary)
    render(reports, summary, args.out)
    print("HTML 报告已生成: %s" % args.out)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary,
                       "apps": [r.to_dict() for r in reports]},
                      fh, ensure_ascii=False, indent=2)
        print("JSON 结果已生成: %s" % args.json)

    # CI 友好的退出码: 有漏检或误报则非零
    sys.exit(0 if summary["total_missed"] == 0
             and summary["false_positives"] == 0 else 1)


if __name__ == "__main__":
    main()
