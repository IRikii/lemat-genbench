# mybench

逐帧 benchmark MatterGen 去噪轨迹的独立脚本集。
**核心区别：invalid 的结构也会出现在结果里。**

## 为什么不用 `lemat_data/experiments/run_benchmarks_v.3.py`

旧脚本对一条 10 帧的轨迹只输出 **1 行** CSV，另外 9 帧无声消失。原因有两层：

1. 上游 `ValidityPreprocessor` 把三项 validity 检查串在同一个 `try` 里，
   电荷中性检查遇到 Md 抛 `KeyError` 后，另两项根本没机会运行；
2. 抛异常的帧被移出 `processed_structures`，而旧脚本从不读 `failed_indices`。

完整分析见 [`notes/20260828-KNOWN_ISSUES.md`](notes/20260828-KNOWN_ISSUES.md)。

mybench 通过**逐项独立调用 metric** 绕开这些问题，`src/` 下的上游包代码一行没改。

## 用法

```bash
# 主用例：读 extxyz 轨迹，每 400 帧抽一帧
.venv/bin/python mybench/run_traj_benchmark.py \
    --traj lemat_data/experiments/mattergen_results/generated_trajectories/gen_0.extxyz \
    --stride 400 --name gen0_run --output-dir temp

# 只看 validity，跳过 MLIP（几秒出结果）
... --no-mlip

# 打开结构弛豫（默认关）
... --relax
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--traj` | — | MatterGen `.extxyz` 轨迹文件（帧 i 对应去噪步 i+1） |
| `--cifs` | — | 备用输入：`gen_{batch}_step_{step}.cif` 所在目录 |
| `--stride` | 200 | 隔多少帧取一帧，取样位置 `frames[stride-1::stride]` |
| `--config` | `comprehensive` | 配置名或 yaml 路径，只用来读 validity 阈值和 MLIP 配置 |
| `--name` | 必填 | 运行标签，出现在输出文件名和图标题里 |
| `--output-dir` | `temp` | 输出目录 |
| `--no-mlip` | 关 | 只跑 validity（此模式不出 HTML 图） |
| `--relax` | 关 | 结构弛豫 `fmax=0.02, steps=50`，额外输出 15 列 |

## 输出

每次运行产出三个文件 `{name}_batch_{batch_id}_{timestamp}.{csv,html,meta.json}`：

- **CSV** — 每帧一行。单点模式 30 列，`--relax` 模式 45 列。
- **HTML** — 可交互的 Plotly 三面板图（Ef / E_hull / 平均力），约 5 MB，自包含。
- **meta.json** — 本次运行的标量元数据（config、stride、模型版本、每个 MLIP 的成功帧数、耗时）。

### 三种 status

| status | 含义 |
|---|---|
| `valid` | 三项检查都跑完且都通过 |
| `invalid` | **至少一项明确不通过**（如原子重叠）——一票否决，其余项是 NaN 也不影响 |
| `undetermined` | 没有不通过项，但**至少一项没能算出来**——证据不全，不能断言合格 |

`undetermined` 基本都是被 `KeyError: 'Md3+'` 挡住的帧。修好上游那个 bug 后，
它们会重新落到 `valid` 或 `invalid`。

### 读表注意

- `charge_deviation` 是**偏差**，越小越好，`0` 表示完全配平（`≤ 0.1` 算通过）；
- `distance_pass` / `plausibility_pass` / `charge_pass` 是**布尔**，`True` = 通过；
  三项 `*_pass` 在对应检查崩溃时是 NaN（"未知"，既非 True 也非 False）。
- `stable` = `E_hull_mean ≤ 0`，`metastable` = `E_hull_mean ≤ 0.1`
  （与上游 `StabilityMetric` 判据一致，`stable=True` 蕴含 `metastable=True`）；
  少于 2 个 MLIP 成功时两列都是 NaN。
- `mlip_note` 为空表示三个模型都成功；否则形如 `mace=no_energy;uma=no_energy`。

## 模块

| 文件 | 职责 |
|---|---|
| `run_traj_benchmark.py` | CLI 入口：读帧、抽样、编排、存 CSV/HTML/JSON |
| `validity.py` | 三项 validity 指标逐项独立调用，组装 status |
| `mlip.py` | 多 MLIP 单点/弛豫计算，按帧名合并回主表 |
| `plotting.py` | Plotly 三面板轨迹图 |
| `notes/` | 设计存档、上游问题记录、测试结论 |

## 已知限制

- 含 Md（及 Po/At/Fr/Fm/No/Lr）的帧，`charge_deviation` 恒为 NaN——上游 bug，见 notes；
- 同样这些帧的 MLIP 列也是 NaN——MACE-MP / orb-omat / UMA-omat 只覆盖到 Z≈89，Md 是 Z=101；
- 当前只处理单条轨迹；跨轨迹聚合见 `notes/20260829-Plan-mybench.md` 的下一步计划。
