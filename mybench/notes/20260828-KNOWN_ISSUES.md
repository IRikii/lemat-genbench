# Known issues (上游 lemat-genbench)

**记录日期**：2026-08-28
**状态**：本次**不修**，仅记录在案。

## 这份文件的来龙去脉

这里记录的问题是在使用**旧脚本 `lemat_data/experiments/run_benchmarks_v.3.py`**
跑 MatterGen 去噪轨迹时暴露出来的。

具体触发场景：拿 `gen_0_cif/` 里 10 帧去噪轨迹做 benchmark，
最终 CSV 只有 **1 行**，另外 9 帧无声消失。追查下去发现是下面的原因 A + 原因 B 叠加。

正因为这些问题——以及旧脚本的设计（只保留 valid 结构）与
「逐帧 benchmark、invalid 结构也必须出现在结果里」这个目标不匹配——
才决定**不在旧脚本上打补丁**，而是新建 `lemat_data/mybench/` 这个独立目录重写整套代码。

新代码通过**绕开**这些问题来工作（逐项调用 metric 而不是用 `ValidityPreprocessor`），
`src/` 下的上游包代码一行都没改。

---

## 原因 A：氧化态概率表查不到就抛异常

### 崩溃链路

```
ValidityPreprocessor.run()
  └─ src/lemat_genbench/preprocess/validity_preprocess.py:283  process_structure()
      └─ src/lemat_genbench/preprocess/validity_preprocess.py:365
             ChargeNeutralityMetric.compute_structure()
          └─ src/lemat_genbench/metrics/validity_metrics.py:187
                 compositional_oxi_state_guesses()
              └─ src/lemat_genbench/utils/oxidation_state.py:163
                     type(comp).oxi_prob[str(Species(el, o))]
                  → KeyError: 'Md3+'
```

### 根因

`src/lemat_genbench/utils/oxidation_state.py:163` 用**裸下标**查氧化态先验概率表：

```python
scores.append(type(comp).oxi_prob[str(Species(el, o))])
```

这张表是 `data/lemat_icsd_oxi_dict_probs.json`，325 条，形如
`{"Cl-": 0.9945, "Tl+": 0.8694, "Tl3+": 0.1306, ...}`，
键是「元素+氧化态」，值是该氧化态在实测晶体结构中出现的频率，
统计自 **ICSD**（Inorganic Crystal Structure Database）+ LeMat-Bulk。

Md（钔，Z=101）是人工合成的超铀元素，从来没有人做出过含 Md 的晶体并解析结构，
ICSD 里一条都没有，所以表里对 Md 完全空白。
代码取 `Element("Md").icsd_oxidation_states` 得到空元组，退回
`common_oxidation_states = (3,)`，于是查 `"Md3+"` → KeyError。

### 与 pymatgen 上游的差异

该函数的注释写明 "Adapted from the `_get_oxi_state_guesses` function from
Pymatgen.core.Composition"。上游原版（`pymatgen/core/composition.py:1117`）是：

```python
score = sum(type(self).oxi_prob.get(Species(el, o), 0) for o in oxid_combo)
```

用 `.get(..., 0)`：查不到 → 先验概率 0（"这个氧化态从没被观测到"），程序继续。
移植时改成了 `[...]`，默认值丢失，语义从"概率为零"变成了"程序崩溃"。

### 受影响的元素

扫过整张周期表，`icsd_oxidation_states or common_oxidation_states` 中
存在表里没有的条目的元素共 **7 个**：

```
Po, At, Fr, Fm, Md, No, Lr
```

全是无 ICSD 实测晶体结构的放射性元素。
（对比：Th、U、Np、Pu 等锕系元素**在**表里，因为它们有实测结构。）

### 复现方式

任何含上述 7 个元素之一的结构过 `ChargeNeutralityMetric` 即可，例如
`gen_0_cif/gen_0_step_200.cif`（Md8Cl）→ `KeyError: 'Md3+'`。

### 这在 MatterGen 轨迹上不是特例，是系统性问题

对 16 条轨迹在 stride=400 下各抽 5 帧的普查结果：

```
gen_0   最终 TlCdRhCl6      4/5 帧含 Md
gen_1   最终 La2Nd4YEr2     4/5
gen_3   最终 Ti3Be2Hg       4/5
gen_11  最终 SrPrZnHg       2/5
gen_13  最终 CoCl6          2/5
...（其余 11 条均为 3~4/5）
```

**每一条轨迹的早期和中期帧都含 Md，最终帧则全部不含。**
Md 是扩散过程中原子种类变量尚未收敛时的"噪声态"——MatterGen 对原子类型也做扩散，
早期类型基本随机，Md 恰好落在元素表末端附近。

所以在当前上游实现下，**任何一条 MatterGen 轨迹跑 validity 都只会剩最后几帧**。

### 将来的修法

对齐上游，一行：

```python
# src/lemat_genbench/utils/oxidation_state.py:163
- scores.append(type(comp).oxi_prob[str(Species(el, o))])
+ scores.append(type(comp).oxi_prob.get(str(Species(el, o)), 0))
```

---

## 原因 B：一项检查崩溃拖垮整帧，失败帧又被丢弃

这是让原因 A 后果放大的结构性问题，三处叠加：

1. **三项检查共用一个 `try`**
   （`src/lemat_genbench/preprocess/validity_preprocess.py:363-373`）——
   电荷中性一崩，最小原子间距和物理合理性**根本没机会运行**，
   而这两项对含 Md 的结构其实完全算得出来。

2. **失败帧被移出结果**
   （`src/lemat_genbench/preprocess/validity_preprocess.py:283-295`）——
   异常帧只记进 `failed_indices`，不进 `processed_structures`。
   `BasePreprocessor` 的通用路径同样如此
   （`src/lemat_genbench/preprocess/base.py:345-347` 过滤掉 None）。

3. **调用方不读 `failed_indices`**——旧脚本的 `validity_check()` 只取
   `processed_structures`，失败帧就此蒸发。日志里 "all N frames retained"
   的 N 是**输入**数，与实际返回数无关，具有误导性。

### 实测：三项里只有一项会崩

对 gen_0 的 10 帧逐项直接调用：

```
 step formula          charge         dist  plaus
  200 Md8Cl            CRASH 'Md3+'      0      1
  400 Md7Cl2           CRASH 'Md3+'      0      1
  600 Md7Cl2           CRASH 'Md3+'      0      0
  800 Md2Cl            CRASH 'Md3+'      0      0
 1000 Md2Cl            CRASH 'Md3+'      0      1
 1200 Md4Cl5           CRASH 'Md3+'      1      1
 1400 Md3RhCl5         CRASH 'Md3+'      0      1
 1600 Md3RhCl5         CRASH 'Md3+'      1      1
 1800 Md2CdRhCl5       CRASH 'Md3+'      1      1
 2000 TlCdRhCl6                   0      1      1
```

9 帧里 **6 帧"原子重叠"、2 帧"物理不合理"是能明确判定的真实结论**，
在当前实现下全部被丢弃。

### mybench 的绕开方式

不用 `ValidityPreprocessor`，改为三次独立的 `BaseMetric.compute()` 调用。
`src/lemat_genbench/metrics/base.py:382-402` 本身就逐结构 try/except，
失败填 NaN 且保持 `individual_values` 与输入等长——正是所需的语义，无需改上游。

---

## 附带问题：MLIP 侧的静默失败

`MACECalculator.calculate_energy_forces`
（`src/lemat_genbench/models/mace/calculator.py:128-154`）内部包了 `try`，
底层报错时**不抛异常**，只记一条 `logger.error` 然后返回 `energy=None, forces=None`：

```python
except Exception as e:
    logger.error(f"MACE calculation failed: {str(e)}")
    return CalculationResult(energy=None, forces=None, stress=None, ...)
```

实测（MACE-MP，Md8Cl）：

```
energy/forces -> "成功"返回，但 energy = None
formation_E   -> ValueError: unsupported operand type(s) for /: 'NoneType' and 'float'
e_above_hull  -> ValueError: unsupported format string passed to NoneType.__format__
```

根本原因是 MACE-MP 只覆盖 89 个元素、最大 Z = 94，不含 Md(101)。
但这个真实原因只出现在 calculator 内部那条 `logger.error` 里；
向上传播的 `ValueError` 措辞完全看不出元素不支持，最终到 CSV 只剩一个 NaN。

mybench 的应对：CSV 里加 `mlip_note` 列（如 `mace=no_energy`），
并在日志里打每个 MLIP 的成功帧数汇总。

---

## 另记一处非崩溃差异

同一个移植函数里，评分方式也和上游不同：

| | 表达式 |
|---|---|
| 上游 pymatgen | `score = sum(...)` |
| 本仓库 `oxidation_state.py:164` | `score = math.prod(scores)` |

这不是崩溃原因，但意味着两边挑选"最可能氧化态组合"的口径不一致，
将来对齐上游时需要一并确认是有意为之还是移植笔误。
