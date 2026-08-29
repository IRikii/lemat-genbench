# mybench：让 invalid 帧进入 benchmark 结果

## Context

目标是对一组结构做 benchmark——**即使结构 invalid，也必须出现在最终结果里**。当前
`lemat_data/experiments/run_benchmarks_v.3.py` 做不到：用 `gen_0_cif`（10 帧）实测，
CSV 只有 **1 行**，另外 9 帧无声消失。

脚本自己的 docstring 写着 "invalid frames kept, tagged with overall_valid=False"，
`plot_batch_results()` 里也有画红色 ✗ "Invalid" 标记的代码——那段代码永远不可能触发。

新代码放在 **`lemat_data/mybench/`**——独立于 `lemat_data/experiments/` 下的实验数据，
方便单独做 git 管理。直接读 MatterGen 的 `.extxyz` 轨迹，
`src/` 下的上游包代码**一行都不改**。

> **路径约定**：下文形如 `metrics/validity_metrics.py` 的写法，完整路径都是
> `src/lemat_genbench/metrics/validity_metrics.py`——已安装的 Python 包 `lemat_genbench`
> 的源码根目录，不是 `tests/` 下的测试文件。

---

## 一、问题诊断

### 崩溃链路

```
run_benchmarks_v.3.py : validity_check()
  └─ ValidityPreprocessor.run()
      └─ src/lemat_genbench/preprocess/validity_preprocess.py:283  process_structure()
          └─ src/lemat_genbench/preprocess/validity_preprocess.py:365
                 ChargeNeutralityMetric.compute_structure()
              └─ src/lemat_genbench/metrics/validity_metrics.py:187
                     compositional_oxi_state_guesses()
                  └─ src/lemat_genbench/utils/oxidation_state.py:163
                         type(comp).oxi_prob[str(Species(el, o))]
                      → KeyError: 'Md3+'
```

### 原因 A — 氧化态概率表查不到就抛异常（本次不修，只记录）

**这是什么表？** `data/lemat_icsd_oxi_dict_probs.json`，325 条，形如：

```json
{"Cl-": 0.9945, "Cd2+": 1.0, "Tl+": 0.8694, "Tl3+": 0.1306, "Rh3+": 0.3639, ...}
```

键是「元素 + 氧化态」，值是**先验概率**——这个元素以这种氧化态出现的频率。
比如铊有 +1 和 +3 两种，实际结构里 87% 是 Tl⁺、13% 是 Tl³⁺。

**"被谁观测"？** 这些频率是从 **ICSD**（Inorganic Crystal Structure Database，
无机晶体结构数据库，收录实验测定并解析过的晶体结构）加上 LeMat-Bulk 数据集统计出来的。
"观测过" = 在真实做出来并测过结构的晶体里出现过。

**Md 为什么没有？** 钔（Md，Z=101）是人工合成的超铀元素，半衰期以天计，
历史上总共只制备过极少量原子，从来没有人做出过含 Md 的晶体并解析结构，
所以 ICSD 里一条都没有。表里对 Md 是完全空白。
（对比：Th、U、Np、Pu 这些锕系元素**在**表里，因为它们有实测晶体结构。）

**代码怎么崩的**：`src/lemat_genbench/utils/oxidation_state.py:163` 用**裸下标**查这张表：

```python
scores.append(type(comp).oxi_prob[str(Species(el, o))])
```

Md 的 `icsd_oxidation_states` 是空元组，退回 `common_oxidation_states = (3,)`，
于是去查 `"Md3+"` → 表里没有 → `KeyError`。
扫过整张周期表，会踩同一个坑的有 7 个元素：`Po, At, Fr, Fm, Md, No, Lr`。

**"移植时丢了 `.get` 的默认值" 是什么意思？**
Python 字典有两种取值写法：

| 写法 | 键不存在时 |
|---|---|
| `d[k]` | 抛 `KeyError`，程序中断 |
| `d.get(k, 0)` | 返回默认值 `0`，程序继续 |

上游 pymatgen 的原版（`composition.py:1117`）用的是**后者**：

```python
score = sum(type(self).oxi_prob.get(Species(el, o), 0) for o in oxid_combo)
```

这份代码的函数注释写明 "Adapted from the `_get_oxi_state_guesses` function from
Pymatgen.core.Composition"——也就是**把上游函数抄过来改的**。抄的过程中把 `.get(..., 0)`
改成了 `[...]`，默认值没了。

语义上这是个错误：查不到应该理解为「这个氧化态从来没被观测到，先验概率 = 0」，
而不是「出错了，整个结构算不了」。上游的处理是对的。

### 原因 B — 一项崩溃拖垮整帧，失败帧又被丢弃（本次要解决）

1. `preprocess/validity_preprocess.py:363-373` 把三项检查串在**同一个 `try` 块**里，
   电荷检查一崩，距离和合理性检查根本没机会运行；
2. `preprocess/validity_preprocess.py:283-295` 把异常帧记进 `failed_indices`，
   不放进 `processed_structures`；
3. 脚本的 `validity_check()` 只读 `processed_structures`，**从不读 `failed_indices`**。

三者叠加 = 9 帧凭空消失。

---

## 二、三项 validity 检查在查什么

### 1. ChargeNeutralityMetric（`metrics/validity_metrics.py:52`）— 电荷中性

**为什么要"猜"氧化态？** 因为生成的结构里根本没有氧化态信息。
MatterGen 的 extxyz 注释行是 `Properties=species:S:1:pos:R:3`——
每个原子只有**元素符号 + xyz 坐标**；CIF 同样只有元素和分数坐标。
扩散模型生成的是原子种类、坐标、晶格，**不输出电荷**。

所以要判断电荷是否配平，只能反推：枚举各元素可能的氧化态组合，
用上面那张先验概率表给每种组合打分，挑最可能的一组，看总电荷离 0 有多远。

> ⚠️ **这一项的读数方向和另外两项相反。** 它返回的是**电荷偏差**（越小越好，
> `偏差 ≤ charge_tolerance` 默认 0.1 才算通过），而另两项返回的是
> `1.0 = 通过 / 0.0 = 不通过`。同一张表里 charge 列的 0 是**好**、
> dist/plaus 列的 0 是**坏**，极易看错。
> **解决办法见第七节的 schema 设计**——最终表里不会同时出现这两种方向的数字。

**这一项就是崩溃源。**

### 2. MinimumInteratomicDistanceMetric（`metrics/validity_metrics.py:293`）— 最小原子间距

查有没有原子挨得太近。对每一对原子（`metrics/validity_metrics.py:370-372`）：

```python
min_dist = (0.7 + r_i + r_j) * scaling_factor      # scaling_factor 默认 0.5
if actual_dist < min_dist:  return 0.0             # 一对不合格就整体判 0
```

半径表里没有的元素（如 Md）用默认半径 1.0，于是 Md–Md 阈值 = (0.7+1+1)×0.5 = **1.35 Å**。
实测 gen_0：step 200 最近间距 1.274 Å（< 1.35，判 0），step 1200 是 1.540 Å（判 1）——对得上。

### 3. PhysicalPlausibilityMetric（`metrics/validity_metrics.py:443`）— 物理合理性

"物理合理性" = **5 项低层次的完整性体检**，全过才返回 1.0，否则 0.0：

| # | 检查 | 通过条件 |
|---|---|---|
| 1 | 质量密度 | `0.01 ≤ ρ ≤ 25.0` g/cm³ |
| 2 | 原子数密度 | `1e-5 ≤ N/V ≤ 0.5` atoms/Å³ |
| 3 | 晶格参数 | 体积 > 1 Å³，a/b/c ∈ [1,100] Å，三个角 ∈ (0°,180°) |
| 4 | 格式 | 能成功写出 CIF（round-trip 不报错） |
| 5 | 对称性 | `SpacegroupAnalyzer` 能定出 1–230 之间的空间群 |

它**不判断化学是否合理**，只排除「明显不是一个正经晶体」的东西。

实测 gen_0 里只有 2 帧没过，都是**质量密度超标**（Md 原子量 258 amu 太重）：

```
step 600  Md7Cl2   ρ = 33.93 g/cm³  > 25   → 0
step 800  Md2Cl    ρ = 25.83 g/cm³  > 25   → 0
其余 8 帧 ρ 在 3.97 ~ 21.07 之间       → 1
```

---

## 三、可行性调研：现有类能否直接为我所用

**结论：能。核心逻辑全部可复用，一行上游代码都不用改。**

最关键的发现是 `metrics/base.py:354` 的 `BaseMetric.compute()` **已经内置了**
我们想要的容错行为。

**`metrics/base.py:382-402` 那个逐结构 `try` 是在做什么？**
它不是在"测试"什么，而是**防止一个结构的异常掀翻整批计算**。结构如下：

```python
for idx, structure in enumerate(structures):     # 遍历这一批 N 个结构
    try:
        value = self.compute_structure(structure, ...)   # 算这一个结构
        values.append(value)
    except Exception as e:
        failed_indices.append(idx)               # 记下是第几个失败的
        values.append(float("nan"))              # 占位，保持长度对齐
        warnings.append(str(e))                  # 记下错误原因
```

注意：**一次 `.compute()` 只跑一项检查**。三项检查是三次独立的调用：

```python
ChargeNeutralityMetric().compute(structures)            # 第 1 遍：电荷中性
MinimumInteratomicDistanceMetric().compute(structures)  # 第 2 遍：最小原子间距
PhysicalPlausibilityMetric().compute(structures)        # 第 3 遍：物理合理性
```

所以是 **3 遍 × N 个结构 = 3N 次独立保护**。某一项在某个结构上崩了，
只影响那一格，另外两项照跑——这正是我们需要的。
（你列的三项里，第 2 项准确说是"最小原子间距"，不是"结构合理性"。）

`MetricResult.individual_values` **与输入等长、按下标对齐**。已实测验证（10 帧输入）：

```
ChargeNeutralityMetric            len(individual_values)=10  failed=[0..8]
                                  values=[nan × 9, 0]
                                  warnings[0]="Failed to compute metric for structure 0: 'Md3+'"
MinimumInteratomicDistanceMetric  len(individual_values)=10  failed=[]
                                  values=[0,0,0,0,0,1,0,1,1,1]
PhysicalPlausibilityMetric        len(individual_values)=10  failed=[]
                                  values=[1,1,0,0,1,1,1,1,1,1]
```

也就是说：**不需要自己写 try/except，不需要碰任何私有 API，不需要 `ValidityPreprocessor`。**

### 类清单（完整路径）

| 类 / 函数 | 完整路径 | 判定 |
|---|---|---|
| `ChargeNeutralityMetric` | `src/lemat_genbench/metrics/validity_metrics.py:52` | ✅ 直接用 `.compute(structures)` |
| `MinimumInteratomicDistanceMetric` | `src/lemat_genbench/metrics/validity_metrics.py:293` | ✅ 同上 |
| `PhysicalPlausibilityMetric` | `src/lemat_genbench/metrics/validity_metrics.py:443` | ✅ 同上 |
| `BaseMetric.compute` / `MetricResult` | `src/lemat_genbench/metrics/base.py:354` / `:67` | ✅ 提供逐结构容错 + 下标对齐 |
| `MultiMLIPStabilityPreprocessor` | `src/lemat_genbench/preprocess/multi_mlip_preprocess.py:183` | ✅ 直接用，喂全部帧 |
| `logger` | `src/lemat_genbench/utils/logging.py` | ✅ |
| orb / mace / uma calculators | `src/lemat_genbench/models/orb/`、`models/mace/`、`models/uma/`、`models/registry.py` | ✅ 间接使用，无需改 |
| 形成能 / 凸包参考数据 | `src/lemat_genbench/preprocess/reference_energies.py`、`utils/e_above_hull.py` | ✅ 间接使用 |
| `ValidityPreprocessor` | `src/lemat_genbench/preprocess/validity_preprocess.py:183` | ⛔ **弃用**（原因 B） |
| `OverallValidityMetric` | `src/lemat_genbench/metrics/validity_metrics.py:630` | ⛔ 弃用——内部同样先走电荷检查，遇 Md 整体 NaN |
| `BasePreprocessor` / `PreprocessorResult` | `src/lemat_genbench/preprocess/base.py` | ⚠️ 会过滤失败结构，见注意点 1 |
| `compositional_oxi_state_guesses` | `src/lemat_genbench/utils/oxidation_state.py:48` | ❌ 有 bug（原因 A），本次不修 |

### 三个注意点

**注意点 1：MLIP 那一步也可能整帧丢失，所以合并结果时按名字查表**

`MultiMLIPStabilityPreprocessor` 内部走 `preprocess/base.py` 的 `run()`，
和 validity 那边一样：某一帧算出异常 → 该帧被丢出 `processed_structures`。

**MLIP 算力和能量，会出什么异常？** 我直接拿 MACE 跑了一遍 Md 结构，实测结果：

```
TlCdRhCl6(正常)   energy/forces -> OK  -29.4419 eV
                  formation_E   -> OK  -0.8565 eV/atom
                  e_above_hull  -> OK   0.0136 eV/atom

Md8Cl(含 Md)      energy/forces -> "成功"，但 energy = None   ← 关键
                  formation_E   -> ValueError: unsupported operand type(s) for /: 'NoneType' and 'float'
                  e_above_hull  -> ValueError: unsupported format string passed to NoneType.__format__
```

失败路径是这样的：`MACECalculator.calculate_energy_forces`
（`src/lemat_genbench/models/mace/calculator.py:128-154`）内部自己包了 `try`，
底层因为 Md(Z=101) 不在模型的 89 个元素词表里而报错，但它**不把异常往外抛**，
而是记一条日志然后返回 `energy=None, forces=None` 的结果对象：

```python
except Exception as e:
    logger.error(f"MACE calculation failed: {str(e)}")   # 只记日志，不 raise
    return CalculationResult(energy=None, forces=None, stress=None, ...)
```

对调用方来说，这个函数"成功返回"了，只是里面的值是 `None`。
等到下游拿这个 `None` 去算形成能（要做除法 `None / float`）和
`e_above_hull`（要做字符串格式化）时，才炸出 `ValueError`。

其他可能的异常类型：

| 异常类型 | 原因 |
|---|---|
| 超时 | `preprocess/multi_mlip_preprocess.py:376` 给每个 MLIP 包了 `func_timeout(300s)`，原子重叠的畸形结构容易让邻居搜索爆炸 |
| 数值发散 | 原子间距接近 0 时能量/力可能变成 `inf` / `NaN` |
| 参考数据缺元素 | 形成能要查每个元素的化学势、`e_above_hull` 要查凸包数据集，含 Md 的组分在参考数据里同样不存在 |

好消息是失败被兜了两层：`multi_mlip_preprocess.py:376` 对**每个 MLIP 单独**用
`try` + `func_timeout` 包住，某个模型挂了只是那个模型返回 `None`，不会掀翻整帧。
所以整帧丢失概率低，但不是零。

**一个副作用要留意**：真正的原因（"Md 不在模型元素表里"）只出现在
calculator 内部那一条 `logger.error` 里，而**最终 CSV 里只剩一个 NaN**，
中间那层 `ValueError` 的措辞（`unsupported operand type(s) for /`）
又完全看不出真实原因。四个工作进程的日志还会交叉刷屏，那一条 ERROR 很容易被淹掉。

所以新脚本应该在合并结果时**显式统计并打印每个 MLIP 各成功/失败了多少帧**，
必要时在 CSV 里加一列 `mlip_note`，避免事后追问"这个 NaN 是哪来的"。

**做法**：喂进去 N 帧，出来的结果先建成 `{frame_name: 结果}` 字典，
再按原始 N 帧顺序逐个查——查得到就填数，**查不到就填 NaN**。
这样无论 MLIP 那边丢了几帧，最终表的行数永远等于输入帧数。

**注意点 2：帧的名字能安全穿过多进程，所以"按名字查表"可靠**

依赖 `structure.properties["name"]` 在 MLIP 计算前后不变。已验证：
`process_structure` 第一步是 `structure.copy()`，pymatgen 的 `copy()` 保留 `properties`；
多进程传递结构走 pickle，`properties` 会被完整序列化带过去。

> **pickle** 是 Python 内置的对象序列化机制：把一个内存里的对象（这里是 `Structure`）
> 转成字节流，传给另一个进程后再还原成等价的对象。Python 的多进程之间不共享内存，
> 所有要传的对象都得先 pickle 再还原。关键点是 `properties` 这个普通字典
> 会跟着一起被序列化，不会在跨进程的路上丢掉。

本次实跑中 `name` / `step` 原样返回，证实了这一点。

**注意点 3：配置文件里有几个"相对当前工作目录"的路径，本方案用不到但要知道**

`src/config/comprehensive.yaml` 里有 `cache_dir: "./data"` 和
`js_distributions_file: "data/lematbulk_jsdistance_distributions.json"`——
不以 `/` 开头的路径是相对**你敲命令时所在的目录**解析的，从别的目录启动就找不到文件。
它们只被 distribution benchmark 用到，本方案不涉及。

反过来，我们**会**用到的形成能 / 凸包 / 氧化态数据是安全的：路径写成
`Path(__file__).parents[2] / "data"`（见 `src/lemat_genbench/utils/oxidation_state.py:106-115`），
锚定在**包自己的安装位置**，与你在哪个目录运行无关。所以新脚本放 scratch 上照样能跑。

---

## 四、输入：直接读 extxyz，不再经过 CIF

`lemat_data/experiments/mattergen_results/generated_trajectories/` 下有 16 条完整轨迹
`gen_0.extxyz` … `gen_15.extxyz`。

**`gen_0.extxyz` 含 2000 帧**（每个去噪步一帧），而 `gen_0_cif/` 里那 10 个 CIF
只是每 200 步抽一帧的子采样。已逐帧精确比对确认：

```
extxyz frame[i]  ==  gen_0_step_{i+1}.cif        （晶格、组分、坐标全部吻合）
frame[199] == step_200.cif ✓   frame[1199] == step_1200.cif ✓   frame[1999] == step_2000.cif ✓
```

读取用 `ase.io.read(path, index=":")` 拿到 2000 个 `Atoms`，再用
`pymatgen.io.ase.AseAtomsAdaptor.get_structure()` 转成 `Structure`——两个库都已装好。
**CIF 转换那一步彻底去掉**，帧读取逻辑直接并进 `run_traj_benchmark.py`，不单独建 `frames.py`。

extxyz 注释行只有 `Lattice` / `Properties` / `pbc`，**没有 step 编号也没有能量**，
所以 `step` 取 `帧下标 + 1`。

### 重要发现：Md 不是 gen_0 的偶然，16 条轨迹全中

我把 16 条轨迹在 `stride=400` 下各抽 5 帧看了一遍组分：

```
gen_0   TlCdRhCl6      4/5 帧含 Md    ['Md7Cl2','Md2Cl','Md4Cl5','Md3RhCl5','TlCdRhCl6']
gen_1   La2Nd4YEr2     4/5            ['NdYMd7','NdYErMd6','La2Nd2YErMd3','La2Nd4YErMd','La2Nd4YEr2']
gen_3   Ti3Be2Hg       4/5            ['Md','Md5Hg','Md2TiBe2Hg','Md2TiBe2Hg','Ti3Be2Hg']
gen_11  SrPrZnHg       2/5            ['Md3Zn','PrMdZnHg','SrPrZnHg','SrPrZnHg','SrPrZnHg']
gen_13  CoCl6          2/5            ['Md4Cl3','Md2Cl5','CoCl6','CoCl6','CoCl6']
...（其余 11 条均为 3~4/5）
```

**每一条轨迹的早期和中期帧都含 Md，最终帧则全部不含。**
说明 Md 是扩散过程中原子种类变量还没收敛时的"噪声态"——
MatterGen 对原子类型也做扩散，早期的类型基本是随机的，Md 只是恰好落在元素表末端附近。

**这抬高了原因 A 的重要性**：`KeyError: 'Md3+'` 不是 gen_0 的特例，
而是**每条轨迹上大部分帧都会撞到的系统性问题**。当前实现下，
任何一条轨迹跑出来都只会剩最后几帧——这正是这次要解决的核心。
（也说明将来真要修原因 A 时，收益会很大。）

### `--stride`：是的，就是"隔多少帧取一帧"

`--stride 200` 表示每 200 帧取一帧，取样位置 `frames[stride-1::stride]`，
正好得到 step = 200, 400, …, 2000 这 10 帧，与原来的 CIF 子采样完全对应。

**为什么需要它**：2000 帧全算不现实。

### 关于模型初始化：你的理解基本对，但有个细节

模型**不是每帧都初始化**，但也不是"整个程序只初始化一次"。准确说是
**每个工作进程、每次调用 preprocessor 初始化一次**：

- `MultiMLIPStabilityPreprocessor` 默认 `n_jobs=4`
  （`preprocess/multi_mlip_preprocess.py` 构造函数），会开 4 个子进程并行；
- **每个子进程独立加载 orb / mace / uma 三个模型**（日志里的
  `Loading orb model in worker process...` 出现 4 次就是这个原因），
  也意味着内存里同时有 4 份模型；
- 子进程随 `ProcessPoolExecutor` 上下文退出而销毁，所以**每调用一次 preprocessor
  就要重新加载一轮**。老脚本每个 batch 调一次 → 每个 batch 重新加载一轮。
- 特例：待算结构数 ≤ 1 时走串行路径，在主进程里只加载一份
  （这就是上次 gen_0 跑 183 s 的情况——当时只有 1 帧通过 validity 进到 MLIP）。

新设计里**一条轨迹只调用一次** preprocessor，所以初始化只发生一轮。
每帧的边际成本目前还没有干净的实测数据（上次只有 1 帧进了 MLIP），
所以先用 `--stride 400`（5 帧）跑一次，从日志里量出"每帧多少秒"，再决定全量用多大 stride。

---

## 五、目录与模块设计

```
lemat_data/mybench/
├── README.md                        # 用途、用法、与 run_benchmarks_v.3.py 的关系
├── notes/                                # 笔记与设计存档
│   ├── 20260829-Plan-mybench.md          #   本计划的副本
│   ├── 20260828-KNOWN_ISSUES.md          #   旧脚本暴露出的上游问题（见第八节）
│   └── 20260829-mybench-test1-sum.md     #   首次验证 + 单点/弛豫对照的结论（见第九节）
├── run_traj_benchmark.py            # CLI 入口：读 extxyz/CIF、抽帧、编排、存 CSV+HTML
├── validity.py                      # 三项指标逐项调用 → validity DataFrame
├── mlip.py                          # MultiMLIPStabilityPreprocessor 封装 → MLIP DataFrame
└── plotting.py                      # Plotly 轨迹图
```

同目录模块互相 import 天然可用（运行脚本时 Python 会把脚本所在目录加进搜索路径）。
`plotting.py` 的绘图骨架从 `run_benchmarks_v.3.py` 搬（那部分逻辑没问题），
原脚本保持不动作为参照。

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--traj` | 一个或多个 `.extxyz` 轨迹文件（主输入） |
| `--cifs` | 备用输入：一个 CIF **目录**（自动扫 `*.cif`，按 `gen_{b}_step_{s}.cif` 解析批次和步数）。不支持列表文件——目录足够了 |
| `--stride` | 隔多少帧取一帧，默认 200 |
| `--config` | 配置名或 yaml 路径，默认 `comprehensive`（见下） |
| `--no-mlip` | 只跑 validity，跳过 MLIP。几秒出结果，适合快速看 validity 分布 |
| `--relax` | 打开结构弛豫（`fmax=0.02, steps=50`），默认关。用于单点 vs 弛豫的对照实验 |
| `--name` | 本次运行的标签（见下） |
| `--output-dir` | 输出目录 |

### 与上游 `stability` 的关系

**上游的 stability 也是用这三个 MLIP。** `StabilityBenchmark`
（`src/lemat_genbench/benchmarks/multi_mlip_stability_benchmark.py:71`）默认
`mlip_names = ["orb", "mace", "uma"]`，跑的也是 `MultiMLIPStabilityPreprocessor`——
和我们用的是同一套机器。区别在它后面多了一层，以及预处理参数不同：

| | 上游 `stability` | 本方案（MLIP 单点） | 本方案（可选：开弛豫） |
|---|---|---|---|
| MLIP | orb / mace / uma | 一样 | 一样 |
| 预处理器 | `MultiMLIPStabilityPreprocessor` | 一样 | 一样 |
| 弛豫 | `multi_mlip_stability.yaml` 里 `relax_structures: true` | `relax_structures=False`，纯单点 | `--relax` 打开，`fmax=0.02, steps=50` |
| 输出粒度 | **整批聚合**成 ratio / 均值，存 JSON | **逐帧原始值**，存 CSV | 同左，多出弛豫后的列 |
| 用途 | "这批生成结构里有多大比例是稳定的" | "这条去噪轨迹上能量怎么下降的" | "每一帧离最近的局部极小有多远" |

### 为什么我们存 CSV 而上游存 JSON

两边选择不同不是随意的，是**数据形状**决定的：上游输出是**一小撮嵌套的标量**
（`stable_ratio` + 每个 MLIP 的版本 + 不确定度 + 配置快照），天然是层级 dict，
JSON 合适；我们输出是**每帧一行的同构二维表**，天然是表格。

实测（用本方案真实的 30 列 schema，2000 行）：

| 格式 | 2000 帧 | 相对 CSV | 16 条轨迹全量外推 |
|---|---|---|---|
| CSV | 754 KB | 1.00× | 11.8 MB |
| JSON (`orient="records"`) | 1313 KB | **1.74×** | 20.5 MB |
| Parquet | 333 KB | **0.44×** | 5.2 MB |

**体积**：JSON 每一行都要把 30 个键名重复写一遍，比 CSV 大 74%。
Parquet 是列式存储 + 压缩，只有 CSV 的 44%。

**后续分析**：

- **CSV** — `pd.read_csv()` 一行读成 DataFrame；能直接用 Excel / `head` / `grep` 看；
  多条轨迹的结果 `pd.concat` 就能拼。缺点：类型信息不落盘，
  `bool` 列一旦混进 NaN，读回来会变成 `object`（我们的 `charge_pass` / `stable` 恰好就有 NaN），
  分析时要显式指定 dtype。
- **JSON** — 读成表格要 `json_normalize`，多绕一步；不能流式追加；
  但存**嵌套**数据很自然。
- **Parquet** — 保留 dtype（bool + NaN 原样回来，不会退化成 object），
  读写快，体积最小。缺点：二进制，不能用文本编辑器或 Excel 直接打开。

**结论**：现阶段（单条轨迹、几十到几千行、需要人眼看和手工比对）**用 CSV**。
另外配一个**小 JSON sidecar** 存这次运行的元数据——config 名、stride、
三个模型的 `model_type` / `hull_type`、每个 MLIP 的成功帧数、总耗时、时间戳——
这些是标量元数据，塞进每一行会重复 N 遍，正是 JSON 该干的活。

将来扩展到"16 条轨迹 × 全量帧"（见第八节的下一步计划）时再切 **Parquet**：
那时候是几万行、纯程序化分析、不再需要肉眼看，Parquet 的体积和 dtype 优势才用得上。

### 上游 stability 到底在算什么、怎么判定

**算的性质是 `E_above_hull`（相对凸包的能量，eV/atom），不是力。**
力和总能量只是中间产物：MLIP 先算出总能量 → 减去各元素化学势得到形成能 →
再和该组分在凸包上的最低能量比，得到 `E_above_hull`。

**判定标准**（`src/lemat_genbench/metrics/multi_mlip_stability_metrics.py:236-238` 及
`MetastabilityMetric`）：

| 指标 | 判据 | 含义 |
|---|---|---|
| `stable_ratio` | `E_above_hull ≤ 0` eV/atom | 落在凸包上或凸包下方——热力学上真正稳定 |
| `metastable_ratio` | `E_above_hull ≤ 0.1` eV/atom | 在凸包上方但不超过 0.1 eV/atom——亚稳，实验上常常也做得出来 |

两个 ratio 的分母都是**结构总数（含算失败的 NaN）**，
所以算不出来的结构会拉低 ratio，而不是被排除在外。

默认还要求至少 2 个 MLIP 成功才给出 ensemble 均值
（`min_mlips_required = 2`），否则该结构的 `E_above_hull` 记为 NaN。

参考我们这条轨迹的 step 2000：`E_hull_mean = 0.0155` eV/atom
→ 不满足 `≤ 0`（不算 stable），但满足 `≤ 0.1`（算 metastable）。

`StabilityMetric.compute_structure`
（`src/lemat_genbench/metrics/multi_mlip_stability_metrics.py:152`）本身**不算能量**，
它只是把预处理器写进 `structure.properties` 的 `e_above_hull` 读出来，
再由 `aggregate_results` 折算成比例和均值。也就是说：
**"MLIP 单点计算"就是上游 stability 的前半段**，我们只是不做后半段的聚合。

### 关于弛豫：默认关，但要做一次对照实验

默认 `relax_structures=False`，因为轨迹分析要看的是**每一帧原本的样子**——
弛豫会把每一帧都拉到各自最近的局部极小，不同帧可能被拉到同一个结构上，
轨迹本身的信息就被抹掉了。

但"弛豫到底影响多大"值得实测，所以加一个 `--relax` 开关，并在验证阶段
对同一批帧跑一次单点、跑一次弛豫，对比：

- **单点 vs 弛豫后的 `E_hull`**：差值就是每一帧"离最近局部极小有多远"，
  这本身是个有意义的量（可以看作一种结构质量指标）；
- **弛豫是否让不同帧收敛到同一结构**：如果 step 1600 和 step 2000 弛豫后
  变成同一个东西，就证实了上面"抹掉轨迹信息"的担心；
- **成本差**：弛豫最多 50 步优化，每帧成本大致是单点的几十倍，
  实测一下才知道全轨迹跑不跑得起。

打开弛豫时预处理器会额外写入 `relaxed_e_above_hull`、`relaxation_energy`、
`relaxation_steps`、`relaxation_rmse`（见 `preprocess/multi_mlip_preprocess.py:544-556`），
对照实验的 CSV 里把这几列一并存下来。

### `--config`：保留，但语义收窄

上游的 `--config` 配合 `--families` 用来选跑哪些 benchmark 家族
（validity / distribution / novelty / stability …）。新脚本的职责固定
（validity + MLIP 单点），所以 `--config` 只用来读**参数**，实际用到两块：

- `validity_settings`：`charge_tolerance`、`distance_scaling`、密度上下限等阈值
- `preprocessor_config.mlip_configs`：三个 MLIP 的 `model_type` / `hull_type`

#### `model_type` 和 `hull_type` 分别是什么

先澄清一点：**选用哪几个 MLIP 是由 `mlip_names` 决定的**（`["orb","mace","uma"]`），
不是 `model_type`。`model_type` 是**同一个 MLIP 家族内部的具体权重版本**：

```yaml
orb:   model_type: orb_v3_conservative_inf_omat    # orb 的 v3-conservative-inf-omat 权重
mace:  model_type: mp                              # MACE 的 MP 版权重（MACE-MP）
uma:   task: omat                                  # UMA 用 task 指定，omat 任务头
```

**`hull_type` 指的是"拿哪一套凸包当参照物"。** 算 `E_above_hull` 要把这个结构的能量
和"同组分体系已知的最低能量包络（凸包）"比。问题是：凸包本身也是用某种方法算出来的。
本地缓存里就有四套：

```
dft_above_hull_*              ← 用 DFT 能量建的凸包
orb_conserv_inf_above_hull_*  ← 用 orb 自己预测的能量建的凸包
mace_mp_above_hull_*          ← 用 MACE-MP 预测的能量建的
uma_above_hull_*              ← 用 UMA 预测的能量建的
```

**为什么要配套**：每个 MLIP 都有自己的系统性偏差。如果拿 orb 预测的能量去和
DFT 建的凸包比，得到的 `E_above_hull` 里混进了"orb 与 DFT 的系统差"，
而不是纯粹的"这个结构离稳定有多远"。**同源相减，系统误差才会抵消。**
所以配置里严格一一对应：

| MLIP | `model_type` | `hull_type` |
|---|---|---|
| orb | `orb_v3_conservative_inf_omat` | `orb_conserv_inf` |
| mace | `mp` | `mace_mp` |
| uma | `task: omat` | `uma` |

（`hull_type: "dft"` 也是合法选项，用于想拿 DFT 凸包当统一标尺的场合。
上游 `models/base.py:74` 的默认值就是 `"dft"`，是当前脚本显式覆盖成同源的。）

想"只确认 validity"用 `--no-mlip`，比换 config 更直接。

**`--name` 出现在哪里**：输出文件名和图标题。当前脚本里是
`f"{run_name}_batch_{batch_id}_{timestamp}.csv"`（`.html` 同理）和图标题
`f"Denoising trajectory · gen_{batch_id} · {run_name}"`。新脚本沿用。

### `validity.py` — 核心

```python
METRICS = {
    "charge":       ChargeNeutralityMetric,
    "distance":     MinimumInteratomicDistanceMetric,
    "plausibility": PhysicalPlausibilityMetric,
}
# 每个 metric 独立 .compute(structures)，拿 individual_values（与输入等长）
# + failed_indices / warnings 组装错误信息列
```

### `mlip.py` — 全部帧都算

不做 valid 过滤，抽样后的全部帧都喂给 `MultiMLIPStabilityPreprocessor`
（`relax_structures=False`、`extract_embeddings=False`，与现脚本一致），
再按注意点 1 的办法按名字合并回主表。

---

## 六、`invalid` 与 `undetermined` 的区别

**为什么会出现 NaN**：`BaseMetric.compute()` 遇到某一帧算不出来（抛异常）时，
不中断整批，而是在那个位置填 `NaN` 占位、把帧下标记进 `failed_indices`。
所以 NaN 的含义是 **"这项检查没能跑完，结论未知"**，
和 "跑完了，结论是不合格"（0.0）是两码事。
本例中 9 帧的 charge 列是 NaN，就是因为 `KeyError: 'Md3+'`。

**判定规则（一票否决，NaN 不算通过）**：

| 情况 | `status` | `valid` | 含义 |
|---|---|---|---|
| 任意一项**明确不通过** | `invalid` | `False` | 已有确凿证据说明它不是好结构（如原子重叠），别的项是 NaN 也不影响这个结论 |
| 没有不通过项，但**有 NaN** | `undetermined` | `False` | 跑完的项都过了，但至少一项没算出来，**不能下"合格"结论**——证据不全 |
| 三项**齐全且全通过** | `valid` | `True` | 三项都真的跑完并通过 |

用 gen_0 实测数据举三个例子：

- **step 200**（charge 算不出, dist=**0** 不通过, plaus=1 通过）→ `invalid`。
  电荷算不出来无所谓，因为原子间距 1.274 Å 已经确凿不合格。
- **step 1200**（charge 算不出, dist=1 通过, plaus=1 通过）→ `undetermined`。
  距离和合理性都过了，但电荷中性完全没算成，无法断言它 valid。
- **step 2000**（charge 偏差 = 0 ≤ 0.1 通过, dist=1 通过, plaus=1 通过）→ `valid`。
  三项齐全且全过。

`undetermined` 单独列出来的价值：一眼看出**哪些帧是被工具链缺陷挡住的**
（修好原因 A 之后它们会重新落到 valid 或 invalid），而不是和真正不合格的结构混在一起。

---

## 七、输出 schema

### 怎么避免 "0 是好还是坏" 的误解

根源是把两种方向的数字塞进同一张表。解决办法：**判定列一律用布尔，
唯一的数值列名字自带方向。**

关键观察：`distance_score` 和 `plausibility_score` 的取值**只可能是 0.0 或 1.0**
（见第二节两个 metric 的实现，都是 `return 1.0 / return 0.0`），
它们当浮点数存没有任何额外信息量。直接转成 `True` / `False`，歧义就消失了：

| 列 | 类型 | 读法 |
|---|---|---|
| `charge_deviation` | float | 电荷偏差，**越小越好**，列名自带方向；`0` 表示完全配平 |
| `charge_pass` | bool / NaN | `charge_deviation ≤ tolerance` |
| `distance_pass` | bool | `True` = 没有原子挨太近 |
| `plausibility_pass` | bool | `True` = 5 项体检全过 |

这样表里**再也没有 0/1 混着两种方向的情况**：所有"通过与否"都是 `True/False`，
唯一的数字是 `charge_deviation`，而它的列名已经说明了"这是偏差，越小越好"。
三个 `*_pass` 列在对应检查崩溃时是 `NaN`（既不是 True 也不是 False，表示"未知"）。

### `status` 和三个 `*_pass` 的关系（为什么两者都要，以及去掉 `valid`）

三个 `*_pass` 是**明细**，`status` 是**汇总**——不是重复，因为汇总不是简单的"与"运算：

```
charge_pass=NaN, distance_pass=False, plausibility_pass=True   → status = invalid
charge_pass=NaN, distance_pass=True,  plausibility_pass=True   → status = undetermined
charge_pass=True,distance_pass=True,  plausibility_pass=True   → status = valid
```

前两行的三个明细列**不同**，但如果只做布尔与运算都会得到"非 True"，
区分不出"确凿不合格"和"没算出来"。`status` 就是把第六节那套一票否决 + NaN 处理
的规则固化成一列，省得每次分析都重推一遍。

**`valid` 列去掉**——你提得对，它就是 `status == "valid"`，纯冗余。
需要布尔筛选时写 `df.status == "valid"` 即可。（旧脚本有这列，新表不再保留。）

### 完整列表（单点模式，30 列）

```
# 标识（4）
name, step, formula, n_atoms

# validity 汇总 + 明细（6）
status,                                    # valid / invalid / undetermined
charge_deviation, charge_pass,             # 唯一的数值列 + 布尔判定
distance_pass, plausibility_pass,          # 纯布尔
validity_error                             # 仅崩溃项非空

# MLIP 形成能（6）
Ef_orb, Ef_mace, Ef_uma, Ef_mean, Ef_std, Ef_n_mlips

# MLIP 凸包上能量（6）
E_hull_orb, E_hull_mace, E_hull_uma, E_hull_mean, E_hull_std, E_hull_n_mlips

# MLIP 平均力（5）
forces_orb, forces_mace, forces_uma, forces_mean, forces_std

# 稳定性判定（2）
stable, metastable

# 诊断（1）
mlip_note
```

合计 4 + 6 + 6 + 6 + 5 + 2 + 1 = **30 列**。

`*_n_mlips` 是有几个 MLIP 成功贡献了这个均值（预处理器本来就会写进
`structure.properties`，见 `preprocess/multi_mlip_preprocess.py:655`），
直接决定下面的稳定性判定可不可信。

### 稳定性判定列

按第五节确认的上游判据，用 ensemble 均值 `E_hull_mean`：

| 列 | 判据 | 说明 |
|---|---|---|
| `stable` | `E_hull_mean ≤ 0` | 落在凸包上或下方 |
| `metastable` | `E_hull_mean ≤ 0.1` | 亚稳。**注意 `stable=True` 的帧 `metastable` 也是 True**（≤0 蕴含 ≤0.1），与上游 `metastable_ratio` 的口径一致 |

`E_hull_n_mlips < 2` 或 `E_hull_mean` 为 NaN 时，两列都是 **NaN**（未知），
不写 False——沿用第六节"算不出来 ≠ 不合格"的原则。
含 Md 的帧就属于这种情况。

### 开弛豫时额外的 15 列（`--relax`，共 45 列）

预处理器在 `relax_structures=True` 时会多写一批属性
（`preprocess/multi_mlip_preprocess.py:580-589`），对应新增：

```
# 弛豫后的形成能 / 凸包上能量，与单点同样的 per-MLIP + mean + std 规格（10）
relaxed_Ef_orb, relaxed_Ef_mace, relaxed_Ef_uma, relaxed_Ef_mean, relaxed_Ef_std
relaxed_E_hull_orb, relaxed_E_hull_mace, relaxed_E_hull_uma,
relaxed_E_hull_mean, relaxed_E_hull_std

# 弛豫过程诊断，只取 ensemble 均值（3）
relaxation_energy_mean,    # 弛豫放出的能量 = 单点能 - 弛豫后能量，越大说明原结构离极小越远
relaxation_steps_mean,     # 实际迭代步数（上限 50），到 50 说明没收敛
relaxation_rmse_mean       # 弛豫前后原子位移的 RMSE，结构被改动了多少

# 弛豫后的稳定性判定（2）
relaxed_stable, relaxed_metastable
```

对照实验最关心的量就是 `E_hull_mean` 与 `relaxed_E_hull_mean` 之差，
以及 `relaxation_rmse_mean`（结构被拉动了多远）。

### `mlip_note` 怎么写

目的：让人看着 NaN 就知道是哪个模型、因为什么没算出来，不用回头翻日志。
所以要**紧凑、可 grep、可机器解析**，而不是一大段英文。

格式：分号分隔的 `模型=状态` 对，**全部成功时留空**（绝大多数行都是空的，不占眼睛）：

| 情况 | `mlip_note` 内容 |
|---|---|
| 三个模型都成功 | *（空字符串）* |
| MACE 返回 `energy=None` | `mace=no_energy` |
| 三个模型都没算出能量 | `orb=no_energy;mace=no_energy;uma=no_energy` |
| MACE 超时 | `mace=timeout` |
| 形成能算不出（能量有但参考数据缺元素） | `mace=no_formation_energy` |
| 整帧在 preprocessor 里丢了 | `all=frame_dropped` |

状态词固定用这几个：`no_energy` / `no_formation_energy` / `no_hull` / `timeout` /
`frame_dropped`，避免自由发挥。原始异常文本太长且具误导性
（`unsupported operand type(s) for /` 完全看不出真实原因是元素不支持），所以不直接塞进来，
只写归类后的短标签；完整堆栈留在日志里。

配套：跑完在日志里打一行汇总，例如
`MLIP summary: orb 1/5 ok, mace 1/5 ok, uma 1/5 ok (4 frames contain unsupported elements)`。

### 绘图（`plotting.py`）

**它是独立模块，但由 `run_traj_benchmark.py` 调用。** 分工是：
`run_traj_benchmark.py` 负责读帧、编排、拼出最终 DataFrame、存 CSV，
最后一步把这个 DataFrame 交给 `plotting.py` 里的函数生成图对象并写盘。
拆成单独文件只是为了让绘图代码不和数据处理逻辑搅在一起，方便以后单独调样式——
从使用者角度看就是跑一次 `run_traj_benchmark.py`，CSV 和图一起出来。

**输出是一个 `.html` 文件**（`fig.write_html()`），不是 PNG/JPG 这类静态图片。
它是一张**可交互的网页**：浏览器打开后可以缩放、平移、悬停看每个点的数值、
点图例开关某条曲线。代价是文件比较大——把 plotly 的 JS 库整个内嵌进去了，
实测约 4.7 MB，但完全自包含，拷到任何机器上双击就能看，不需要联网或装环境。

（想要静态 PNG 的话需要额外装 `kaleido`，当前 venv 里**没有**。
如果需要，我可以加，或者你直接在浏览器里用 plotly 自带的相机图标导出 PNG。）

图的内容沿用三子图（Ef / E_hull / 平均力）+ ±std 色带 + 三条 MLIP 曲线。
原来「无效帧画在 `y=0` 加红 ✗」的做法要换掉——0 在能量图上是有意义的数值，
9/10 帧无效时整张图会被假点淹没。改为 `fig.add_vline(x=step, row=..., col=1)` 竖直标记：

- `invalid` → 橙色虚线
- `undetermined` → 灰色点线
- 各配一条 `visible="legendonly"` 的哑 trace 提供图例

MLIP 曲线保持 `connectgaps=False`（plotly 默认），NaN 处自然断开。

---

## 八、笔记文件

### `lemat_data/mybench/notes/20260828-KNOWN_ISSUES.md`

**开头先交代来龙去脉**：这份文件记录的是在使用**旧脚本
`lemat_data/experiments/run_benchmarks_v.3.py`** 跑 MatterGen 去噪轨迹时
暴露出来的问题。正是因为这些问题——以及旧脚本的设计与"逐帧 benchmark、
invalid 结构也要保留"这个目标不匹配——才决定不在旧脚本上打补丁，
而是新建 `lemat_data/mybench/` 这个独立目录重写整套代码。
其中的上游 bug（原因 A）本次**不修**，只记录在案。

正文内容：

- 崩溃链路与 `src/lemat_genbench/utils/oxidation_state.py:163` 根因
- 那张概率表是什么、Md 为何缺席（第一节的解释）
- 与 pymatgen 上游 `.get(Species(el, o), 0)` 的差异
- 受影响的 7 个元素：`Po, At, Fr, Fm, Md, No, Lr`（均无 ICSD 数据）
- 复现方式：任何含这些元素的结构过 `ChargeNeutralityMetric`
- 连带影响：三项检查共用一个 `try`（`preprocess/validity_preprocess.py:363-373`），
  一项崩溃导致另两项的真实结论被丢弃；`BasePreprocessor` 再把整帧过滤掉
- MLIP 侧的静默失败：calculator 内部吞掉异常返回 `energy=None`，
  真实原因只留在一条 `logger.error` 里（第三节注意点 1）
- 将来的修法：对齐上游改成 `.get(str(Species(el, o)), 0)`，一行
- 另记一处非崩溃差异：上游 `score` 用 `sum`，这份用 `math.prod`
  （`src/lemat_genbench/utils/oxidation_state.py:164`），评分口径不同

### `lemat_data/mybench/notes/20260829-Plan-mybench.md`

本计划原样复制一份，留作设计存档，方便日后回看"这套代码最初是怎么设计的"。
文件末尾追加一节 **下一步计划**：

> 当前脚本的作用域是**单个生成结构的去噪轨迹**——看一条轨迹上性质如何随去噪步演化。
>
> 下一步要把同样的 benchmark 工作**扩展到多个结构**（现有 16 条轨迹
> `gen_0.extxyz` … `gen_15.extxyz`，乃至更大批量），从"看单条轨迹"升级为
> **评估整个结构生成模型**：它的去噪轨迹整体上是如何优化性质的。
> 届时需要考虑的：跨轨迹的统计聚合方式（每个去噪步上的均值/分位数）、
> 不同轨迹长度的对齐、以及计算量（16 条 × 2000 帧显然要靠 stride 和 GPU）。

---

## 九、验证

1. **快速链路检查**——先跑 `--no-mlip`，几秒出结果，确认 validity 三列和 status 分类正确。

2. **主用例**——`--stride 400` 取 5 帧，一次跑通即可（不再跑 stride 200 的 10 帧）：
   ```bash
   .venv/bin/python lemat_data/mybench/run_traj_benchmark.py \
       --traj lemat_data/experiments/mattergen_results/generated_trajectories/gen_0.extxyz \
       --stride 400 --config comprehensive --name gen0_mybench --output-dir temp
   ```
   这 5 帧同时覆盖三种 status，已用实测数据确认预期输出：

   | step | formula | `charge_deviation` | `charge_pass` | `distance_pass` | `plausibility_pass` | `status` | `stable` | `metastable` |
   |---|---|---|---|---|---|---|---|---|
   | 400 | Md₇Cl₂ | NaN | NaN | False | True | `invalid` | NaN | NaN |
   | 800 | Md₂Cl | NaN | NaN | False | False | `invalid` | NaN | NaN |
   | 1200 | Md₄Cl₅ | NaN | NaN | True | True | `undetermined` | NaN | NaN |
   | 1600 | Md₃RhCl₅ | NaN | NaN | True | True | `undetermined` | NaN | NaN |
   | 2000 | TlCdRhCl₆ | 0.0 | True | True | True | `valid` | False | True |

   逐项核对：

   - CSV **5 行 × 30 列**（当前实现只会给 1 行）；
   - 4 个含 Md 的帧 `validity_error` 含 `Md3+`，
     `mlip_note` 为 `orb=no_energy;mace=no_energy;uma=no_energy`（待实跑确认三个都失败）；
   - `distance_pass` / `plausibility_pass` 5 帧全部是真实的 True/False，没有 NaN；
   - 4 个含 Md 的帧 `stable` / `metastable` 应为 **NaN**（`E_hull_n_mlips = 0`），
     不是 False；
   - step 2000 `E_hull_mean = 0.0155` → `stable = False`、`metastable = True`。

   顺便从日志量出模型初始化耗时和每帧边际耗时。

   同一份输出上再核对两件事（**不需要另外跑**，都是看这个 CSV）：

   **2a. 回归**——`step=2000`（TlCdRhCl₆）的 MLIP 数值应与之前 CIF 版跑出来的**完全一致**：
   `Ef_mean = -0.8306`、`E_hull_mean = 0.01548`、`forces_mean = 0.4285`。
   你的判断是对的：`relax_structures=False`（`run_benchmarks_v.3.py:363`），
   走的是**单点计算**，没有弛豫，所以是确定性的、应当逐位吻合。
   （如果哪天开了弛豫，优化路径受初始条件和数值细节影响，就只能要求近似一致了。）

   **2b. Md 帧的 MLIP 行为**——已实测：MACE 对含 Md 的结构返回 `energy=None`，
   下游形成能 / `e_above_hull` 抛 `ValueError`，被逐 MLIP 的 `try` 兜住 → 该列 NaN。
   orb-v3-omat / UMA-omat 训练集元素范围相同，预期一致。
   要确认的是**整帧不被丢弃**（靠注意点 1 的按名字查表），而不是 NaN 本身：
   那 4 个含 Md 的帧必须出现在 CSV 里，MLIP 列为 NaN，validity 列有真实值。

3. **单点 vs 弛豫 对照实验**——**换用 `gen_13.extxyz`**。
   gen_0 在 stride 400 下只有 1 帧不含 Md，能对比的样本太少；
   上面的普查显示 `gen_13`（最终 CoCl₆）和 `gen_11`（最终 SrPrZnHg）
   各有 **3 帧不含 Md**，是 16 条里最合适的。

   同一批帧跑两次，一次默认单点、一次 `--relax`：
   ```bash
   .venv/bin/python lemat_data/mybench/run_traj_benchmark.py \
       --traj .../gen_13.extxyz --stride 400 --name gen13_sp     --output-dir temp
   .venv/bin/python lemat_data/mybench/run_traj_benchmark.py \
       --traj .../gen_13.extxyz --stride 400 --name gen13_relax --relax --output-dir temp
   ```
   先核对形状：单点那份仍是 **30 列**，`--relax` 那份应是 **45 列**
   （多出第七节列的 15 列弛豫相关字段），行数两份都等于抽帧数。

   然后对比两份 CSV，结论写进
   **`lemat_data/mybench/notes/20260829-mybench-test1-sum.md`**：
   - 每帧 `E_hull` 单点值 vs 弛豫后值的差（= 该帧离最近局部极小有多远）；
   - 弛豫后 step 1200 / 1600 / 2000 这三帧是否收敛到同一结构
     （它们本来就都是 CoCl₆，比对弛豫后的晶格和坐标即可）——
     若确实变成同一个东西，就证实了"弛豫会抹掉轨迹信息"；
   - 两次运行的墙钟时间差，估算全轨迹开弛豫是否跑得起。

   #### `20260829-mybench-test1-sum.md` 的结构

   **第一模块 · 背景**——说明这次跑的是**首轮验证测试，不是正式结果**，目的有三个：
   ① 确认新脚本整条链路跑得通、输出 schema 正确（invalid 帧确实被保留）；
   ② 对比单点计算与弛豫后计算的差异，为"要不要默认开弛豫"提供依据；
   ③ 实测计算资源消耗（模型初始化耗时、每帧边际耗时、内存、单点 vs 弛豫的成本比），
   为后续大规模正式测试估算预算。用的是 `gen_13.extxyz`、`--stride 400`、共 5 帧。

   **第二模块 · 测试结果与分析**——上面那三条对比的实际数据和结论，
   外加验证步骤 1、2、2a、2b 的核对结果（行数、列数、status 分布、NaN 分布是否符合预期）。

   **第三模块 · 执行日志**——按时间顺序的流水记录，简单即可：

   ```
   2026-08-29 HH:MM  创建 lemat_data/mybench/ 目录结构
   2026-08-29 HH:MM  完成 validity.py / mlip.py / plotting.py / run_traj_benchmark.py
   2026-08-29 HH:MM  跑 --no-mlip 快速链路检查，通过
   2026-08-29 HH:MM  启动 gen_13 单点测试（5 帧）
   2026-08-29 HH:MM  单点测试完成，耗时 X 分
   2026-08-29 HH:MM  启动 gen_13 弛豫测试（5 帧）
   2026-08-29 HH:MM  弛豫测试完成，耗时 Y 分
   2026-08-29 HH:MM  ruff check / format 通过
   ```

   目的是日后回看时知道"这套代码和结论是什么时候、按什么顺序做出来的"。

4. **Lint**——"lint" 指静态代码检查。`ruff check` 扫出未使用的导入、未定义的变量、
   import 排序错误这类问题；`ruff format` 统一代码格式（缩进、引号、换行）。
   仓库已配好 ruff（见 `pyproject.toml` 的 `[tool.ruff.lint]`）：
   ```bash
   .venv/bin/ruff check lemat_data/mybench/
   .venv/bin/ruff format lemat_data/mybench/
   ```

### 预期产出的文件

**四次验证运行，每次产出同一组三个文件**，命名沿用
`{name}_batch_{batch_id}_{timestamp}.{ext}`（batch_id 取自 `gen_{N}.extxyz` 的 N）：

| 验证步骤 | `--name` | 落在 `temp/` 的文件 |
|---|---|---|
| 1. 快速链路检查<br>`gen_0 --stride 400 --no-mlip` | `gen0_nomlip` | `gen0_nomlip_batch_0_<ts>.csv`（5 行，MLIP 列全 NaN）<br>`gen0_nomlip_batch_0_<ts>.meta.json` |
| 2. 主用例<br>`gen_0 --stride 400` | `gen0_mybench` | `gen0_mybench_batch_0_<ts>.csv`（5 行 × 30 列）<br>`gen0_mybench_batch_0_<ts>.html`（≈5 MB 交互图）<br>`gen0_mybench_batch_0_<ts>.meta.json` |
| 3a. 对照-单点<br>`gen_13 --stride 400` | `gen13_sp` | `gen13_sp_batch_13_<ts>.csv`（5 行 × 30 列）<br>`.html` + `.meta.json` |
| 3b. 对照-弛豫<br>`gen_13 --stride 400 --relax` | `gen13_relax` | `gen13_relax_batch_13_<ts>.csv`（5 行 × **45 列**）<br>`.html` + `.meta.json` |

验证步骤 **2a / 2b 不单独跑**——它们是对步骤 2 那份 CSV 的核对，不产生新文件；
步骤 4（lint）也不产生文件。

外加每次运行的终端日志重定向文件 `temp/<name>.log`（用来量初始化耗时和每帧耗时）。

`--no-mlip` 模式**不出 HTML**——三个子图画的全是 MLIP 量，没有 MLIP 数据时是三张空图，
没有意义。

**总计**：`temp/` 下约 4 个 CSV + 3 个 HTML + 4 个 JSON + 4 个 log = 15 个文件，
体积主要是三个 HTML（每个约 5 MB，plotly 的 JS 内嵌），合计 ~15 MB。

### 哪些该留、哪些该删

- **`temp/` 里的东西全是一次性的**，随时可删。
- **例外**：步骤 3 的两份 CSV（`gen13_sp` 和 `gen13_relax`）是
  `20260829-mybench-test1-sum.md` 里各项结论的原始证据。建议把这两个 CSV
  （只要 CSV，HTML 太大不必留）复制一份到
  `lemat_data/mybench/notes/20260829-test1-data/`，日后回看结论时能核对数字。
- **笔记文件本身**（第八节的三个 `.md`）写在 `lemat_data/mybench/notes/` 下，
  不是临时产物，不要删。

除上述两个 CSV 副本外，`temp/` 由你手动清理。

---

## 十、下一步计划

当前脚本的作用域是**单个生成结构的去噪轨迹**——看一条轨迹上性质如何随去噪步演化。

下一步要把同样的 benchmark 工作**扩展到多个结构**（现有 16 条轨迹
`gen_0.extxyz` … `gen_15.extxyz`，乃至更大批量），从"看单条轨迹"升级为
**评估整个结构生成模型**：它的去噪轨迹整体上是如何优化性质的。

届时需要考虑的：

- **跨轨迹的统计聚合方式**——每个去噪步上取均值 / 中位数 / 分位数带，
  还是先对每条轨迹归一化再聚合；
- **不同轨迹长度的对齐**——目前 16 条都是 2000 步，若将来采样步数不同，
  需要按相对进度（step / total_steps）对齐；
- **计算量**——16 条 × 2000 帧 = 32000 帧，显然要靠 stride 控制，
  并且应该上 GPU 节点（本轮测试是在 CPU 登录节点上跑的）；
- **存储格式**——那时候是几万行、纯程序化分析，应从 CSV 切到 Parquet
  （见第五节的实测对比：体积约为 CSV 的 44%，且保留 bool + NaN 的 dtype）；
- **原因 A 的修复优先级**——普查显示每条轨迹的早中期帧几乎都含 Md，
  不修的话大批帧的电荷中性检查会一直是 `undetermined`。

---

## 存档说明

本文件是 `/home/users/nus/ruiqiche/.claude/plans/invalid-structure-benchmark-invalid-iterative-summit.md`
在 2026-08-29 实施前的副本，留作设计存档，方便日后回看这套代码最初是怎么设计的、
以及每个决定背后的理由。实施过程中的偏差记录在
`20260829-mybench-test1-sum.md` 的执行日志一节。
