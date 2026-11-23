# 应用文件更新 · 崩溃一致性检测工具

本地状态存储/配置/元数据/小日志服务普遍假设"先写临时文件再 rename"就是安全的。
真实断电暴露两类测试环境永远碰不到的问题：

1. **rename 生效了但临时文件内容还在页缓存里** —— 空文件替换掉完好的旧文件，服务重启起不来；
2. **append/新建文件后没同步目录** —— 目录项整体回滚，文件整个不见。

本工具用一个**带页缓存语义的仿真文件系统**跑被测应用，枚举每个断电时刻磁盘上
可能出现的全部状态，对每个状态执行应用自己的恢复逻辑并校验其声明的一致性条件，
最后用 delta-debug 归约出最小问题重排并定位缺失的同步调用。

## 持久化模型（仿真与真实内核一致的部分）

| 平面 | 操作 | 持久化由谁保证 |
|---|---|---|
| 数据面 | write / append / truncate | `fsync(文件)` |
| 目录面 | create / rename / delete | `fsync(父目录)` |

- 同步调用是**屏障**：它之前的同平面操作保证落盘；屏障之后的操作可任意重排、部分落盘或全丢。
- 两个平面之间**没有任何顺序保证**：rename（目录面）先于文件数据（数据面）落盘是合法的磁盘状态。
- 单次 write/append 自身原子（整块在或整块不在）；truncate 与后续 write 是两个独立单位，
  这就是"覆盖写掉电变空文件"的机制。
- 操作按 `(平面, 对象)` 分组，组间独立持久化 —— 重排空间 = 各组待落盘操作的子集笛卡尔积。

## 目录结构

```
crashcheck/
  model.py        页缓存文件系统仿真、操作记录、屏障语义、崩溃回放
  enumerator.py   崩溃状态枚举: 全枚举 / 危险重排优先 / 剪枝与偏向抽样 / 朴素对照
  checker.py      恢复执行、一致性校验、delta-debug 最小化、同步缺失定位与修复建议
  report.py       单文件离线 HTML 报告
  apps/           9 个被测应用(4 类更新模式 × 缺陷/正确写法)
run_evaluation.py 验收驱动(检出率/误报/朴素对比/状态数/耗时)
tests/            21 个单元与验收测试
```

## 被测应用与预埋缺陷

| 应用 | 更新模式 | 预埋缺陷标签 |
|---|---|---|
| `overwrite_buggy` | 直接覆盖写 | `torn-overwrite` 截断先于写入落盘 → 空文件 |
| `overwrite_correct` | 覆盖写 + fsync | 无 |
| `temprename_buggy` | 临时文件+rename | `rename-empty-replacement`（空文件替换）、`rename-dir-not-fsynced`（目录项丢失） |
| `temprename_partial` | 只 fsync 数据忘了目录 | `rename-dir-not-fsynced` |
| `temprename_correct` | 数据+目录双 fsync | 无 |
| `logckpt_buggy` | 追加日志+检查点 | `acked-append-lost`（确认写入丢失）、`torn-checkpoint`（空快照拒绝启动） |
| `logckpt_correct` | append 同步+原子检查点 | 无 |
| `wal_buggy` | 写前日志 | `wal-commit-not-durable`（COMMIT 晚于主数据）、`main-torn-write` |
| `wal_correct` | COMMIT fsync 后再改主文件 | 无 |

## 使用

```bash
# 完整验收, 生成 report.html 与 results.json
python3 -I run_evaluation.py --json results.json

# 只看一个应用 / 调整每崩溃点抽样预算
python3 -I run_evaluation.py --app wal_buggy --budget 256

# 单元测试
python3 -I -m unittest discover -s tests -v
```

打开 `report.html`：顶部为验收总览，各 Tab 展示单个应用的**操作序列与同步点**、
**出问题的最小重排**（丢失/保留操作序号、崩溃后磁盘实际内容）、**缺失同步位置与修复建议**。

## 状态爆炸处理

- 待落盘组合数 ≤ 预算时**完整枚举**所有子集（覆盖全部重排）；
- 超预算时先强制注入 5 类危险重排（D1 rename 先于数据、D2 目录项先于数据、
  D3 截断先于写、D4 数据全丢、D5 目录项全丢），再按"目录项易保留、数据易丢失"
  的偏向概率抽样，同崩溃点内按磁盘签名去重；
- 实测 27 个待落盘操作（理论 2²⁷ 组合）在预算 128 下被压到 126 个状态。

## 最新验收结果

```
预埋缺陷 8 个: 检出 8 / 漏检 0, 总检出率 100%
正确写法 5 个: 误报 0
朴素边界断电法: 漏检 2 个(rename-empty-replacement、wal-commit-not-durable)
                —— 二者都是跨平面重排缺陷, 不建模重排就不可能发现
总崩溃状态数 186, 总耗时约 10 ms
```
