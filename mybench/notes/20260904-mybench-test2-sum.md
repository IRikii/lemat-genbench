# mybench 第二轮改动总结（test2）

**日期**：2026-09-04
**执行环境**：登录节点，CPU，repo `.venv`
**前置阅读**：`20260829-mybench-test1-sum.md` 的 **2.8 节**（三个出图 bug 的根因分析）

---

## 一、背景

第一轮（8/29）做完 test1 之后留下两笔账，当时**只记录、没动代码**：

1. **出图的三个 bug**（记在 test1 笔记 2.8 节）——`--relax` 的结果根本没画进图、
   点图例只有一个子图响应、invalid/undetermined 的图例项点了没反应。
2. **默认输出路径没跟上目录迁移**——`mybench/` 已从 `lemat_data/mybench/` 搬到仓库根目录
   并纳入 git，跑批产出也已 `mv` 到 `lemat_data/temp/`，
   但 `--output-dir` 的默认值还是 `temp`，下次跑批会在仓库根目录重新造一个出来，
   `git status` 又脏。

本轮把这两笔账清掉。**只动 `plotting.py` / `run_traj_benchmark.py` / `README.md` 三个文件，
`src/` 下的上游包代码依旧一行没改。**

---

## 二、改动内容

### 2.1 给每条 trace 加 `legendgroup`（修 test1 笔记的 Bug 2）

**问题**：每条曲线在三个子图里各画一次、名字相同（三条 `name="orb"`），
但只有第一行 `showlegend=row_idx == 1` 出现在图例里，且**没设 `legendgroup`**。
plotly 只切换被点击的那一条 trace 本身，同组概念不存在，所以点 "orb"
只有第 1 个子图的曲线消失。

**改法**：`showlegend` 逻辑不动，额外给每条 trace 加 `legendgroup`。

| 行为 | 改前 | 改后 |
|---|---|---|
| 点 `orb` / `mace` / `uma` | 只有子图 1 的那条消失 | 三个子图的同名曲线一起消失 |
| 点 `mean` | 同上 | 同上 |
| 点 `±std` | 只隐藏了下界填充那一条，上界隐形线还在 | 色带的上下两条一起切换 |

分组情况（实测 trace 数）：

```
std           6 条   （每子图 2 条：上界隐形线 + 下界填充）
mean          3 条   （每子图 1 条）
orb / mace / uma  各 3 条
invalid       3 条
undetermined  3 条
relaxed       2 条   （只有 Ef 和 E_hull 两个面板）
```

### 2.2 竖线从 shape 改成真 trace（修 Bug 3）

**问题**：`invalid` / `undetermined` 的竖线原来用 `fig.add_vline()` 画，
在 plotly 里属于 **shape（图形注释）**而不是 trace，而 **shape 无法通过图例切换**。
图例里那两项其实是另加的哑 trace（`visible="legendonly"`），点它只切换那条本来就不可见的
哑 trace，竖线纹丝不动；而且 `legendonly` 让它一开始就显示成灰掉的状态，看着像"已隐藏"。

**改法**：每个 status、每个子图画**一条真 `go.Scatter`**，用 `None` 把多段竖线断开：

```
x = [s1, s1, None, s2, s2, None, ...]
y = [lo, hi, None, lo, hi, None, ...]
```

配 `legendgroup="invalid"` / `"undetermined"`、`hoverinfo="skip"`
（不加的话 `hovermode="x unified"` 的悬停框里会多出无意义条目），
并去掉 `visible="legendonly"`，默认可见。

**`lo` / `hi` 怎么定**——取该子图**实际绘制的所有序列**（`mean±std`、三条 MLIP、
以及 relaxed 线）的有限值算 min/max，上下各留 5% 余量。两个退化情况必须处理：

| 情况 | 处理 | 实测 |
|---|---|---|
| 整个面板全是 NaN | 回退到 `[0, 1]`，竖线照样标出位置 | 未在本轮样本中触发 |
| 只有一个有限点（min==max） | 按 `max(\|v\|*0.1, 0.1)` 撑开，避免零高度 | gen_0 的 Ef 面板只有 step 2000 有值，实测高度 0.049，正常 |

因为 y 值是显式给的，plotly 的自动量程会把它们包含进去，与数据范围自洽。

### 2.3 开弛豫时叠加 relaxed 系列（修 Bug 1）

**问题**：`PANELS` 只用 `Ef` / `E_hull` / `forces` 三个**单点**前缀，
全文件对 `relaxed_*` 的引用数是 0，所以开不开 `--relax` 画出来一模一样。

**改法**：`build_figure()` 检测 `"relaxed_Ef_mean" in df.columns`，
有就在 **Ef 和 E_hull 两个面板**各叠一条 `relaxed_{prefix}_mean`：
青色点线 + 菱形点，`legendgroup="relaxed"`，图例名 `relaxed mean`。
图标题也会追加 `· single-point vs relaxed`。

**力的面板不加**，因为根本没有"弛豫后的力"可画：上游
`src/lemat_genbench/preprocess/multi_mlip_preprocess.py:458-461`
先对**原始结构**算 energy/forces，之后（`:495` 起）才做弛豫，
弛豫后的力从未写进 `properties`。这一点直接写在子图标题上：
`Mean force magnitude (eV/A, single-point only)`。

### 2.4 顺带修一个配色冲突

`STATUS_STYLES["invalid"]` 原本是 **`#ff7f0e`，和 mace 曲线完全同色**，
图上橙色虚线和橙色 mace 曲线混在一起分不清。全部换成互不重复的 tab10：

| 元素 | 改前 | 改后 |
|---|---|---|
| orb / mace / uma | `#1f77b4` / `#ff7f0e` / `#2ca02c` | 不变 |
| mean | `#9467bd` | 不变 |
| **relaxed mean** | — | `#17becf` 青（新增） |
| **invalid** | `#ff7f0e`（与 mace 撞色） | `#d62728` 红 |
| **undetermined** | `#888888` | `#7f7f7f`（统一到 tab10） |

### 2.5 新增 `--from-csv`

改一次图原本要重跑完整计算——弛豫那次 **21 分钟**。加了 `--from-csv <path>` 之后：
读现成 CSV、跳过全部计算、只重新生成 HTML。

**它在哪个文件？** 在 **`run_traj_benchmark.py`**，不是 `plotting.py`。
`plotting.py` 只负责"给我一个 DataFrame，我还你一个图对象"，
不关心数据是刚算出来的还是从磁盘读的；
`--from-csv` 属于**入口脚本的输入方式**，所以定义在 `run_traj_benchmark.py`：

- 参数加进已有的互斥组（`run_traj_benchmark.py:279`），
  与 `--traj` / `--cifs` 三者互斥、三选一；
- 走 `replot()`（`:207`）这条短路径：`pd.read_csv` → 解析 batch_id → `build_figure` → 写 HTML；
- `batch_id` 从 CSV 的 `name` 列首行解析（`gen_13_step_400` → 13），复用已有的 `CIF_NAME_RE`；
- `--stride` / `--config` / `--relax` / `--no-mlip` 在此模式下无意义，显式传了会打 warning。

**为什么只写 HTML，不写 CSV 和 meta.json**：重画图不是一次真实运行。
再写一份 CSV 只是原样复制，而 meta.json 里的 `elapsed_seconds`、`mlip_ok_frames`
这些字段会**伪造出"这里跑过一次计算"的记录**，日后翻笔记会被误导。

### 2.6 途中发现并修掉的一个坑：`--from-csv` 喂 `--no-mlip` 的 CSV 会崩

**现象**：

```
KeyError: 'Ef_mean'
```

**为什么会这样**——两个事实撞在一起：

1. **`--no-mlip` 产出的 CSV 只有 10 列**，全是 validity 相关的，一列 MLIP 数据都没有：

   ```
   name, step, formula, n_atoms, status,
   charge_deviation, charge_pass, distance_pass, plausibility_pass, validity_error
   ```

   （对比：正常单点跑批 30 列，`--relax` 47 列。）

2. **`build_figure()` 无条件按列名取数**。它的主循环第一句就是
   （`plotting.py:103`）：

   ```python
   for row_idx, (_, prefix) in enumerate(PANELS, start=1):
       mean_values = df[f"{prefix}_mean"]     # prefix 依次是 Ef / E_hull / forces
   ```

   第一轮迭代 `prefix="Ef"`，于是去取 `df["Ef_mean"]`——
   这一列在 `--no-mlip` 的 CSV 里**根本不存在**，pandas 直接抛 `KeyError: 'Ef_mean'`。

也就是说：图的三个面板画的**全是** MLIP 量（形成能、凸包上能量、力），
而 `--no-mlip` 的 CSV 一个 MLIP 量都没有，**本来就没有图可画**。
正常跑批时这条路走不通是因为 `run_traj_benchmark.py` 里有
`if not args.no_mlip:` 的保护，压根不会去画图；
但 `--from-csv` 是新开的入口，绕过了那道保护。

**改法**：在 `replot()` 里先检查三个关键列，缺了就抛一个说人话的错误：

```
lemat_data/temp/defaultpath_check_....csv has no MLIP columns
(missing Ef_mean, E_hull_mean, forces_mean); it looks like a --no-mlip run,
which produces no figure. Replot a CSV from a run that included MLIP.
```

比原来那个光秃秃的 `KeyError: 'Ef_mean'` 直接告诉你**问题出在输入文件上**，
而不是让人怀疑绘图代码有 bug。

### 2.7 默认输出路径（共 5 处）

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `run_traj_benchmark.py` | `--output-dir` 默认值 | `temp` | `lemat_data/temp` |
| `run_traj_benchmark.py` | docstring 用法示例 | `--output-dir temp` | `--output-dir lemat_data/temp` |
| `run_traj_benchmark.py` | docstring 里的脚本路径 | `lemat_data/mybench/run_traj_benchmark.py`（搬家后的残留） | `mybench/run_traj_benchmark.py` |
| `README.md` | 用法示例 | `--output-dir temp` | `--output-dir lemat_data/temp` |
| `README.md` | 参数表默认值 | `` `temp` `` | `` `lemat_data/temp` `` |

第 3 处是目录搬家后遗留的旧路径，照着 docstring 敲命令会找不到文件，一并修掉。

README 另补了 `--from-csv` 的参数表条目、一个用法示例、一节「图怎么看」；
顺带把 relax 模式的列数从 **45 改正为 47**（45 是第一轮计划里的估算，
实测是 47，test1 笔记 2.7 节记过这个偏差，但 README 当时没跟着改）。

**`mybench/notes/` 下的历史记录一概不动**——那些 `lemat_data/mybench/` 的引用是
当时的事实描述，属于历史存档；目录迁移本身已经记在 `GIT-SETUP.md` 里了。

---

## 三、验证结果

### 3.1 从归档 CSV 重画两张图

```bash
.venv/bin/python mybench/run_traj_benchmark.py \
    --from-csv mybench/notes/20260829-test1-data/gen13_sp_batch_13_20260829_141316.csv \
    --name gen13_sp_v2 --output-dir lemat_data/temp
# 同样方式跑 gen13_relax
```

| 运行 | 墙钟耗时 | 其中 CPU 时间 |
|---|---|---|
| `gen13_sp_v2` | 1 min 24 s | 11.0 s |
| `gen13_relax_v2` | 1 min 3 s | 10.6 s |

墙钟一分钟里绝大部分是 `import lemat_genbench` 那条依赖链（torch / fairchem / pymatgen），
真正的读 CSV + 画图只有十几秒。**对比原来的 21 分钟弛豫跑批，快了约 20 倍。**
两次都只产出一个 `.html`，没有多余的 CSV / meta.json。

### 3.2 图的结构核对（直接检查 `fig` 对象，不靠解析 HTML）

| 检查项 | 单点图 | 弛豫图 | 结论 |
|---|---|---|---|
| trace 总数 | 24 | 26 | 正好多 2 条（relaxed 系列） |
| **shape 数** | **0** | **0** | 竖线全部变成真 trace ✅ |
| `relaxed mean` trace 数 | 0 | 2 | 只在 Ef / E_hull 面板出现 ✅ |
| `visible="legendonly"` 的 trace | 0 | 0 | 无初始灰掉的条目 ✅ |
| 没有 `legendgroup` 的 trace | 0 | 0 | 全部归组 ✅ |

弛豫图上 `relaxed mean` 两条线的实际 y 值，与 CSV 逐位吻合：

```
relaxed_Ef_mean      = [nan, nan, -0.2535, -0.1855, -0.1658]
relaxed_E_hull_mean  = [nan, nan,  0.1959,  0.2640,  0.2837]
```

（`0.1959 / 0.2640 / 0.2837` 正是 test1 笔记 2.5 节那三个弛豫后的 `E_hull` 值。）

图例条目（弛豫图，共 8 项，各出现一次）：

```
±std, mean, relaxed mean, orb, mace, uma, invalid (4), undetermined (1)
```

**注**：一开始想用"解码 HTML 里的 plotly base64 数组"来比对，
但发现 plotly 6 只对部分数组做 base64 编码（含 NaN 的 y 序列走的是普通 JSON），
解出来只有 x 轴那几条，比不全。改成直接在 Python 里 `build_figure()` 后检查
`fig.data` / `fig.layout.shapes`，更直接也更可靠。

### 3.3 图例交互（浏览器人工确认）

已在浏览器里逐项点击确认：三个子图同时响应、竖线可以从图例开关、
没有一开始就灰掉的条目。**交互行为符合预期。**

### 3.4 退化情况

用 `gen0_mybench_batch_0_20260829_140452.csv` 重画（Ef 和 E_hull 面板都只有 1 个有限值）：

| 面板 | 竖线 y 范围 | 高度 |
|---|---|---|
| Ef | −0.8587 ~ −0.8101 | 0.049 |
| E_hull | 0.01341 ~ 0.01701 | 0.0036 |
| forces | −1.359 ~ 37.49 | 38.85 |

三个面板都没塌成零高度，渲染正常。

### 3.5 默认输出路径

不带 `--output-dir` 跑一次 `--no-mlip`（2 帧，约 1 秒）：

- 产物落在 `lemat_data/temp/defaultpath_check_batch_0_20260904_150307.csv` ✅
- 仓库根目录**没有**新建出 `temp/`（`ls -d temp` → No such file）✅
- `git status --short` 只有本轮真正改动的文件：

  ```
   M mybench/README.md
   M mybench/notes/draft-for-0830     ← 本轮之前就有的
   M mybench/plotting.py
   M mybench/run_traj_benchmark.py
  ```

  **没有 `?? temp/`** —— 这正是本轮要保证的结果。

### 3.6 Lint

`.venv/bin/ruff check mybench/` → All checks passed；
`ruff format --check mybench/` → 4 files already formatted。

---

## 四、执行日志

> 时间取自 `date` 实测和文件 mtime。
> （test1 笔记里第二轮那段初版的时间是凭印象编的，已在那边修正并留了说明，这次不再犯。）

```
2026-09-04 11:17  确认目录现状：mybench/ 已在仓库根目录并被 git 跟踪，
                  temp/ 已迁到 lemat_data/temp/；核对计划里的 4 处路径，
                  发现第 5 处（docstring 里的旧脚本路径）
2026-09-04 11:23  改完 plotting.py：legendgroup、竖线改真 trace、
                  relaxed 叠加、配色冲突
2026-09-04 11:24  重画 gen13_sp_v2，耗时 1 min 24 s
2026-09-04 11:33  重画 gen13_relax_v2，耗时 1 min 3 s
2026-09-04 约 11:35  核对 fig 对象：trace 数 24 vs 26、shape 数 0、
                  relaxed 值与 CSV 逐位吻合
2026-09-04 14:45  改完 README.md（用法、参数表、图怎么看、45→47 列修正）
2026-09-04 15:03  默认输出路径验证：产物落 lemat_data/temp/，
                  仓库根目录无 temp/，git status 干净
2026-09-04 16:30  发现 --from-csv 喂 --no-mlip 的 CSV 会抛 KeyError，
                  加了列检查守卫并改成说人话的报错
2026-09-04 16:31  ruff check / format 全部通过
2026-09-04 约 21:20  浏览器里人工确认图例交互正常（你确认）
2026-09-04    21:38  完成本总结文件
```

---

## 五、原始数据

本轮重画的两张 HTML 留在 `lemat_data/temp/`：

- `gen13_sp_v2_batch_13_20260904_112448.html`
- `gen13_relax_v2_batch_13_20260904_113339.html`

属临时产物，可自行清理。数据来源是 `20260829-test1-data/` 下第一轮归档的两份 CSV，
**那两份不动**。

---

## 六、遗留

- test1 笔记 2.8 节写的"本轮只记录、未修改代码"针对的是 8/29 那一轮，
  按约定**不回改历史笔记**；三个 bug 的修复情况以本文件为准。
- `--from-csv` 目前要求 `--name` 必填。如果嫌每次重画都要起名麻烦，
  以后可以让它默认沿用源 CSV 的文件名前缀。
