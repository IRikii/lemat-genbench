# 剔除占位符原子帧 —— 实施前的设计存档

**日期**：2026-09-10（设计）/ 2026-09-11（实施）
**产出**：`mybench/sanitize_trajectories.py`、`mybench/run_traj_benchmark_nomd.py`、
`mybench/plotting.py` 的一处向后兼容改动、
`20260910Test-sing-traj-w.o.Md/run_16traj_nomd.pbs`
**前置阅读**：`20260828-KNOWN_ISSUES.md`（`KeyError: 'Md3+'` 根因、7 元素清单）、
`20260905Test-sing-traj-for-16str/test_results.md` 第四节（CUDA 粘性错误、丢帧证据）

---

## 一、背景

第一轮实验（`20260905Test-sing-traj-for-16str`）对 16 条 MatterGen 去噪轨迹
按 `--stride 200` 各抽 10 帧做逐帧 benchmark。validity 侧的信号很干净——
valid 比例随去噪步单调上升，step 600 时 0/16，step 2000 时 15/16。

但 **MLIP 侧 160 帧里只有 25 帧（16%）拿到了能量和力**，其余 135 帧
`mlip_note = all=frame_dropped`。

根因：Md（Z=101）不在模型的元素嵌入表里。**在 CPU 上**这个越界是干净的 Python
异常（`101 is not in list`），被逐 MLIP 的 `try` 兜住；**在 GPU 上**触发的是
**device-side assert**，这是个**粘性错误**——一旦触发，该工作进程的整个 CUDA
上下文就废了，之后它处理的每一帧全部陪葬。

证据（test_results.md 第四节）：

```
不含 Md 的 34 帧：保留 25、被丢 9      ← 这 9 帧是无辜受害者
含   Md 的 126 帧：保留 0、被丢 126
```

`n_jobs=4`、10 帧 → 批次划分 `[200,400,600] [800,1000,1200] [1400,1600,1800] [2000]`，
第 3 批的第一帧含 Md，1600 和 1800 跟着陪葬；第 4 批只有 step 2000 一帧，
从头到尾没碰过 Md，所以算出来了。**哪些帧能活取决于它碰巧被分到哪个批次，本质上是随机的。**

那份笔记列的 5 个候选办法里，第 2 个是
"**跑之前先过滤掉含不支持元素的帧，不让它们进 MLIP，从源头避免污染**"。
本次就是落地这条。

---

## 二、被排除的元素及理由

排除**这 7 个元素**，**整帧剔除，不是剔除原子**——
只把 Md 原子删掉会留下一个在那个去噪步上从未存在过的结构。

```python
PLACEHOLDER_ELEMENTS = ("Md", "Po", "At", "Fr", "Fm", "No", "Lr")
```

| 元素 | Z | 为什么排除 |
|---|---|---|
| **Md** | 101 | MatterGen 对原子类型也做扩散，Md 是类型变量尚未收敛时的**占位符 / mask token**，根本不是预测结果。两处后果：① MLIP 元素嵌入表没有它，GPU 上触发粘性 CUDA assert 并污染整个进程；② `charge` 检查抛 `KeyError: 'Md3+'`。 |
| Po | 84 | 以下 6 个与 Md 同类：全是**无 ICSD 实测晶体结构**的放射性元素。`Element(x).icsd_oxidation_states` 为空 → 退回 `common_oxidation_states` → 查 `oxi_prob` 表时 `KeyError`（上游 `src/lemat_genbench/utils/oxidation_state.py:163` 用了裸下标，pymatgen 原版是 `.get(..., 0)`）。同时也超出 MLIP 的元素覆盖——`element_chem_pot.json` 只有 89 个元素（`src/lemat_genbench/preprocess/reference_energies.py:98-114`）。 |
| At | 85 | 同上 |
| Fr | 87 | 同上 |
| Fm | 100 | 同上 |
| No | 102 | 同上 |
| Lr | 103 | 同上 |

**本批 16 条轨迹里实际只有 Md 出现**（见下节普查），另外 6 个是对将来别的轨迹的保险，
放进来不会误伤本批数据。清单是模块级常量，可用 `--placeholder-elements` 覆盖。

对比一下：Th、U、Np、Pu 等锕系元素**在**表里，因为它们有实测结构，不受影响。

---

## 三、普查结果（决定了设计）

命令（可复现）：

```bash
.venv/bin/python mybench/sanitize_trajectories.py \
    --traj-dir lemat_data/experiments/Bench-mattergen16str/mattergen_test_batches/generated_trajectories
```

| trajectory | n_total | n_placeholder | n_clean | first_clean_step | last_clean_step | contiguous | n_atoms | clean_formula | elements |
|---|---|---|---|---|---|---|---|---|---|
| gen_0 | 2000 | 1924 | 76 | 1925 | 2000 | yes | 9 | CdCl6RhTl | Cd Cl Rh Tl |
| gen_1 | 2000 | 1882 | 118 | 1883 | 2000 | yes | 9 | Er2La2Nd4Y | Er La Nd Y |
| gen_2 | 2000 | 1428 | 572 | 1429 | 2000 | yes | 8 | Br2Nd4PdPt | Br Nd Pd Pt |
| gen_3 | 2000 | 1840 | 160 | 1841 | 2000 | yes | 6 | Be2HgTi3 | Be Hg Ti |
| gen_4 | 2000 | 1424 | 576 | 1425 | 2000 | yes | 18 | Cd11Hg2Li3Mg2 | Cd Hg Li Mg |
| gen_5 | 2000 | 1642 | 358 | 1643 | 2000 | yes | 8 | Cd6Ga2 | Cd Ga |
| gen_6 | 2000 | 1938 | 62 | 1939 | 2000 | yes | 20 | Li4Se12Ta4 | Li Se Ta |
| gen_7 | 2000 | 1950 | 50 | 1951 | 2000 | yes | 12 | La4Nd4Sn4 | La Nd Sn |
| gen_8 | 2000 | 1872 | 128 | 1873 | 2000 | yes | 11 | Cs2Dy2HfSe6 | Cs Dy Hf Se |
| gen_9 | 2000 | 1212 | 788 | 1213 | 2000 | yes | 4 | ErLiPdSm | Er Li Pd Sm |
| gen_10 | 2000 | 1950 | 50 | 1951 | 2000 | yes | 18 | Ce2Li10Tl6 | Ce Li Tl |
| gen_11 | 2000 | 1082 | 918 | 1083 | 2000 | yes | 4 | HgPrSrZn | Hg Pr Sr Zn |
| gen_12 | 2000 | 1958 | **42** | 1959 | 2000 | yes | 12 | Bi2P2PrPt7 | Bi P Pr Pt |
| gen_13 | 2000 | 970 | **1030** | 971 | 2000 | yes | 7 | Cl6Co | Cl Co |
| gen_14 | 2000 | 1924 | 76 | 1925 | 2000 | yes | 8 | Al2Zn6 | Al Zn |
| gen_15 | 2000 | 1770 | 230 | 1771 | 2000 | yes | 11 | Ag2CoDy8 | Ag Co Dy |
| **合计** | **32000** | **26766 (83.6%)** | **5234** | | | | | | |

（`clean_formula` 是按元素字母序拼的，与 pymatgen 的 reduced formula 顺序不同，
组分本身一致，例如 gen_12 的 `Bi2P2PrPt7` = pymatgen 的 `PrBi2P2Pt7`。）

三个结论直接决定了设计：

**1. 干净帧远不止"最后几十帧"，共 5234 帧**
最短的 gen_12 只有 42 帧，最长的 gen_13 有 1030 帧，全算是第一轮（160 帧）的 33 倍。
→ **所以要 `--max-frames`（默认 100）在干净段内均匀抽稀。**

**2. 含占位符的帧严格是轨迹前段，干净帧是连续尾段**
16 条全部 `contiguous = yes`，没有出现"消失又复现"。
代码里仍然逐帧判断、不假设连续，但会检查并在 meta.json 里记 `clean_contiguous`，
不连续就打 warning。

**3. 干净段内组分完全恒定，且不含任何其他问题元素**
每条轨迹的干净尾段只有 1 种组分（表里 `clean_formula` 列都是单值），
这 5234 帧用到的 33 种元素
（Ag Al Be Bi Br Cd Ce Cl Co Cs Dy Er Ga Hf Hg La Li Mg Nd P Pd Pr Pt Rh Se Sm Sn Sr Ta Ti Tl Y Zn）
全部是常规元素，Po/At/Fr/Fm/No/Lr 一个都没出现。
→ 剔除之后 `KeyError: 'Md3+'` 与 CUDA assert **都不该再发生**，
`undetermined` 应当归零、三个 MLIP 的成功帧数应当等于总帧数。这是本轮的验收判据。

换句话说：**干净尾段是原子类型已经收敛、只剩坐标和晶格在演化的那一段。**

---

## 四、设计

### 4.1 `mybench/sanitize_trajectories.py`

双身份：既是 `run_traj_benchmark_nomd.py` 导入的模块，也能独立当普查 CLI 跑。

- `scan_trajectory(path, placeholders, keep_atoms=True) -> SanitizeReport`
  用 `ase.io.iread` **流式**读。不用 `ase.io.read(index=":")` 是因为要丢掉的那 84%
  没必要同时全压进内存。
  **`step = 帧序号 + 1`，与 `run_traj_benchmark.py:98` 完全一致**——
  x 轴用的是原始去噪步号，不重新编号，这样两轮实验的图可以直接对照。
- `select_frames(report, max_frames) -> List[int]` —— 抽稀，返回的是**干净帧列表内部的下标**
  （`0 … M-1`，M = 干净帧数），不是文件里的帧号。

  ```python
  if max_frames <= 0 or M <= max_frames:
      return list(range(M))
  return sorted(set(np.linspace(0, M - 1, max_frames).round().astype(int)))
  ```

  用 `linspace(0, M-1, ...)` 而不是固定步长的 `::k`，就是为了让**两个端点必定入选**——
  它的首末两个值恒等于 `0` 和 `M-1`，正是"首个干净帧"和"step 2000"这两个锚点。

  | 轨迹 | M | `--max-frames` | 取到 | step |
  |---|---|---|---|---|
  | gen_13 | 1030 | 100 | 100 | 971, 981, 992, 1002, … 1990, **2000** |
  | gen_12 | 42 | 100 | 42（全取） | 1959, 1960, … **2000** |
  | gen_12 | 42 | 3 | 3 | 1959, 1979, **2000** |

  （最后一行中间值是 1979 不是 1980：`linspace` 给出 20.5，`np.round` 是 banker's
  rounding 取偶 → 20。端点对就行。）
- `to_structures(report, positions)` —— 转 pymatgen Structure，并写
  `properties["name"] = f"gen_{batch}_step_{step}"`、`properties["step"]`。
  `name` 的格式必须严格一致：它是 validity 表和 MLIP 表的合并键。
- `check_element_coverage(structures)` —— 对 Z > 89 的元素打 warning（软提示，不拦截）。
  占位符过滤已经把已知的挡掉了，这里能抓到的都是意外，值得在日志里看见。

### 4.2 `mybench/run_traj_benchmark_nomd.py`

名字与结果目录 `20260910Test-sing-traj-w.o.Md`、作业名 `gen{N}_nomd` 对齐。
**注意实际过滤的是 7 个元素，Md 只是其中唯一真会出现的那个**——这一点写在脚本
docstring 和 `--placeholder-elements` 的帮助文本里，避免名字误导。

复用现成模块，不复制逻辑：`validity.evaluate` / `mlip.evaluate` / `plotting.build_figure`。
`find_repo_root` / `load_config` / `batch_id_from_frame_names` 这几个小函数是**照抄**
而不是 import 的——`run_traj_benchmark.py` 是 `__main__` 入口，会被 multiprocessing
的 spawn 重新执行一遍（日志里那堆 `_fixup_main_from_path` traceback 就是这么来的），
不去加深这个耦合。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--traj` | — | `.extxyz`，与 `--from-csv` 互斥、必选其一 |
| `--from-csv` | — | 从既有 CSV 重画图，跳过全部计算，只出 HTML |
| `--max-frames` | `100` | 干净帧超过它就在干净段内均匀抽稀；`0` = 全取 |
| `--placeholder-elements` | 7 元素 | 逗号分隔，覆盖默认清单 |
| `--config` | `comprehensive` | 同旧脚本 |
| `--name` | 必填 | 同旧脚本 |
| `--output-dir` | `lemat_data/temp` | 同旧脚本 |
| `--no-mlip` | 关 | 只跑 validity（不出图） |
| `--relax` | 关 | 透传给 `mlip.evaluate` |

**没有 `--stride`**：本实验用不上，要便宜地试跑就 `--max-frames 3`。
**没有 `--cifs`**：那些 CIF 只有 step 200…2000 的 10 帧，且几乎全含 Md。

干净帧为 0 时：打 `logger.error`，写一份只含 sanitize 记录的 meta.json，`sys.exit(1)`——
在 16 路数组作业里，那个文件是区分"这条轨迹没东西可算"和"这个子作业崩了"的唯一凭据。

输出与第一轮**同名同构**（`{name}_batch_{batch}_{timestamp}.{csv,html,meta.json}`，
单点 30 列 / `--relax` 47 列，`step` 仍是原始去噪步号），便于直接对照。
`meta.json` 去掉 `stride`，加 `max_frames` / `n_frames_selected` / `selected_steps`
和一个 `sanitize` 块（占位符清单、总帧/剔除/干净帧数、连续性、首末干净 step、
被剔除的 step 区间、保留帧的元素清单）。

### 4.3 `mybench/plotting.py`：一处向后兼容的改动

`build_figure` 增加可选参数 `tick_every: Optional[int] = None`：

- `None`（默认）→ 每帧一个刻度，**`run_traj_benchmark.py` 的图一个像素都不变**（已实测逐值比对）；
- 传整数 → `tickvals = steps[::tick_every]`，并**强制并入首末两个 step**。

新脚本按实际帧数分三档：

| 帧数 `n` | `tick_every` | 实测标签数 |
|---|---|---|
| `n ≤ 30` | 1 | 每帧一个（10 帧那种小图行为不变） |
| `31 ≤ n ≤ 50` | 5 | 42 帧 → 10 个 |
| `n > 50` | 10 | 100 帧 → 11 个 |

标签是**步数本身**。`mode="lines+markers"` 暂不动——先看 100 帧的图乱不乱再说。

### 4.4 明确不动的部分

- `mybench/run_traj_benchmark.py` —— 一行不改，第一轮的命令逐字重跑结果不变
- `mybench/validity.py` / `mybench/mlip.py` —— 一行不改，直接复用
- `src/` 下的上游包代码 —— 一行不改。
  `oxidation_state.py:163` 那个 `.get(..., 0)` 的修法**本轮不做**：
  剔除占位符帧之后它已经碰不到了，混在一起改会让"这轮的改善来自哪"说不清。

---

## 五、验证

**A. 静态检查**
`.venv/bin/ruff check mybench/` + `ruff format --check mybench/` —— 全过。

**B. 普查 CLI 自证**
输出与规划期间用**独立的 awk 流式统计**做出的表逐格一致（5234 干净帧、83.6%、
33 种元素、每条的首个干净 step 与组分全对）。两条互不相干的实现对上了，普查逻辑才算可信。

**C. 登录节点 dry check（CPU，`--no-mlip`，0.5 s）**

```bash
.venv/bin/python mybench/run_traj_benchmark_nomd.py \
    --traj .../generated_trajectories/gen_12.extxyz \
    --max-frames 3 --name dry_gen12 --output-dir lemat_data/temp --no-mlip
```

实测：3 行；`step = 1959, 1979, 2000`（端点钉住）；`status` 全 `valid`、
**没有 `undetermined`**（第一轮 gen_12 那 10 帧里有 7 帧 invalid）；
`formula` 全是 `PrBi2P2Pt7`；`meta.json` 的 `sanitize` 块记下
`n_frames_with_placeholder=1958`、`placeholder_step_ranges=[[1,1958]]`。

**D. 出图刻度**
`tick_every_for` 三档边界实测（30→1、31→5、50→5、51→10）；
100 帧 → 11 个标签且首末为 971 / 2000；42 帧 → 10 个标签且 2000 被强制并入；
缺省路径的 `tickvals` 与改动前逐值相同。

**E. 交互式 GPU pilot**（待做）

先在登录节点起 tmux 再 `qsub -I`——`qsub -I` 挂在当前终端上，断线即杀作业：

```bash
tmux new -s nomd_pilot
qsub -I -N nomd_pilot -q normal \
     -l select=1:ncpus=4:mem=32gb:ngpus=1 -l walltime=01:00:00

# 节点上
export HF_HOME=/scratch/users/nus/ruiqiche/lemat_data/hf_cache
cd /home/users/nus/ruiqiche/lemat-genbench
.venv/bin/python mybench/run_traj_benchmark_nomd.py \
    --traj .../gen_12.extxyz --max-frames 100 --name pilot_gen12 --output-dir lemat_data/temp
.venv/bin/python mybench/run_traj_benchmark_nomd.py \
    --traj .../gen_13.extxyz --max-frames 100 --name pilot_gen13 --output-dir lemat_data/temp
```

判据：`mlip_ok_frames` 三个模型都等于 100（第一轮是 4/10）；日志里 `device-side assert`
出现 0 次（第一轮每份日志 39~90 次）；`mlip_note` 列全空；`status_counts` 只有
`valid`/`invalid`；`selected_steps` 首末为 971 和 2000；实测耗时用来确认 walltime。

**F. 全量数组作业**（待做）
`qsub 20260910Test-sing-traj-w.o.Md/run_16traj_nomd.pbs`。
按 `--max-frames 100` 算的工作量：`42+50+50+62+76+76 + 100×10 = 1356` 帧，单条最多 100 帧。

---

## 六、PBS 脚本里踩过的坑

1. **第一轮脚本的 `TRAJ_DIR` 已失效** —— 它指向
   `experiments/mattergen_results/generated_trajectories`，这个目录现在不存在了；
   轨迹实际在 `Bench-mattergen16str/mattergen_test_batches/generated_trajectories`。
   照抄不改会让 16 个子作业全部撞上 `ERROR: trajectory not found`。
2. **`#PBS -o` 要求 `logs/` 在 qsub 之前就存在**，`mkdir -p` 一步不能省。
3. `walltime` 从 `02:00:00` 降到 `00:40:00`（第一轮实测只用了申请量的 1%，
   排队优先级会更好）。注意 `gdev` 队列上限是 2 h。
