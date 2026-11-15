# 大模型代码检查测评系统 · llm-code-review-bench

在自己项目的代码上，横向比较不同产品 / 大模型「找 bug」的能力。

**核心思路**：同一套提示词、同一份代码、同一套字段 → 各家作答 → 匿名化去重 → 交叉裁决 → 计分。

不需要现成的标准答案。真相来自三处：**跑得出来的复现、跨族裁判的票、你自己的眼睛**。

> A self-hosted benchmark for LLM code-review capability. Same prompt, same frozen code,
> same output schema for every candidate; dedupe by cluster, adjudicate blind, score with
> a recomputable layer. Zero third-party dependencies, Python ≥ 3.11. Docs are in Chinese.

---

## 为什么不用现成的榜单

1. **公开榜单测的是别人的题，不是你的代码。** 你的项目有自己的技术栈、自己的坑；在 HumanEval 上分高，不代表能在你的 FastAPI 鉴权中间件里找出越权。
2. **「报得多」和「报得准」在榜单上分不开。** 有的模型一道题报 12 条，7 条是命名风格；有的报 3 条，3 条都致命。只看得分不看误报，选型一定翻车。
3. **同一处问题被拆成多条就重复计分**，这件事在没有去重机制的评测里普遍存在（本系统也踩过，见「已知局限」）。

这套系统把评测拆成四层，每层都可独立重算：**采集层（不可变原始作答）→ 归一化与聚类层 → 裁决层 → 计分层**。改评分口径不用重跑模型。

---

## 流程

```
config/projects.toml   ──task──▶  tasks/<id>/prompt.md      冻结代码 + 提示词
                                        │
                    同一个 prompt 分别交给 N 个候选（人工粘贴 / agent 自动跑）
                                        ▼
                            runs/<run>/raw/<候选>/<题>/r1.md
                                        │  normalize（字段校验、超限拒收）
                                        ▼
                            runs/<run>/normalized/*.jsonl
                                        │  cluster（同文件 ±8 行 + 标识符交集）
                                        ▼
                      claims/pool.jsonl（无来源） + clusters.jsonl（去重后）
                                        │
              ┌─────────────────────────┼─────────────────────────┐
        人工 human.jsonl          执行复现测试 unittest        裁判包 packets→votes
              └─────────────────────────┼─────────────────────────┘
                                        ▼
                              judgments/verdicts.jsonl
                                        │  score（可反复重算）
                                        ▼
                        reports/<run>/{report.md,scores.csv,scatter.html}
```

---

## 30 秒试用

```bash
python src/cli.py demo
```

会造一个含 4 个真实缺陷的示例项目，模拟三家作答，跑完全流程，输出：

- `reports/demo-001/report.md`　排行榜与分项
- `reports/demo-001/scatter.html`　能力 × 成本散点
- `reports/demo-001/scores.csv`　进 Excel

不需要任何第三方库，不需要 API Key。

---

## 一次真实评测

### 第 0 步　配置

```bash
python src/cli.py scan "<你的项目目录>" --depth 3
```

按「有 git 历史 / 体量合适 / 有测试 / 近期改过」打分排序，写出 `config/projects.suggested.toml`。挑几个复制进 `config/projects.toml`，再把 `enabled` 改成 `true`；同时编辑 `config/candidates.toml` 登记被测的产品与模型（**`vendor` 一定要填对**，判断「是否同一家」靠它）。

### 第 1 步　建题库

```bash
python src/cli.py new-run r01
python src/cli.py task p01-backend
```

`tasks/p01-backend/prompt.md` 是**已经拼好代码的完整提示词**，打开全选复制即可。

文件按修改时间倒序取，最近改过的优先——冻结两年的老代码，问题早被踩平了。

### 第 2 步　采集（最花时间）

对每个候选、每道题，重复同一个小循环：

```bash
python src/cli.py start r01 "ProductA/model-x" p01-backend
# —— 手动操作：把 prompt.md 全文贴进产品，把回复完整存成 out.md ——
python src/cli.py done r01 "ProductA/model-x" p01-backend --from out.md
```

**关键约束**：所有候选用同一个 task 的 `prompt.md`，一个字都不改。这是可比性的全部来源。

也可以把题目交给具备文件与终端能力的 agent 自跑：仓库里的 `启动.txt`（作答阶段）与 `第二阶段启动.txt`（裁判阶段）就是一键指令，要求它自助登记身份、逐题落盘、写完自检。多模型并发跑同一批次时，**必须各自用不同的候选 id**，否则会互相覆盖。

### 第 3 步　处理

```bash
python src/cli.py normalize r01
python src/cli.py cluster r01
# 先审查 runs/r01/reproductions 里的模型生成测试，再授权执行
python src/cli.py execute r01 --allow-untrusted
python src/cli.py packets r01                       # 生成无来源信息的裁判包
python src/cli.py votes r01 judge-a ./judge-a-out   # 回收裁判票
python src/cli.py verdicts r01
python src/cli.py score r01
python src/cli.py report r01
```

或者审查完复现测试后 `python src/cli.py all r01 --allow-untrusted` 一次跑完。

**安全边界**：`execute` 运行的是模型生成的 Python 代码。测试只与冻结代码一起复制到独立目录、不继承 `PYTHONPATH`，但**这不是安全沙箱**。不可信的测试必须先审查；高风险环境请在容器或一次性虚拟机里跑。

### 第 4 步　人工复核（省不掉）

`runs/r01/judgments/queue.md` 列出待定、裁判票分裂、执行结果冲突，以及需要复核的高严重度条目。填 `runs/r01/judgments/human.jsonl`：

```json
{"cluster_id":"K004","verdict":"valid","reason":"空列表确实会崩"}
{"cluster_id":"K007","verdict":"invalid","reason":"命名偏好，不是缺陷"}
```

```bash
python src/cli.py verdicts r01 && python src/cli.py score r01 && python src/cli.py report r01
```

**只重算，不重跑模型。** 这是这套系统的关键性质：裁决层可反复修改，计分层随之重算，原始作答一次不动。

---

## 四条裁决通道

| 通道 | 条件 | 谁判 | 单条成本 |
|---|---|---|---|
| **人工** | 你填了 `human.jsonl` | 你，永远优先 | ~2 分钟 |
| **执行结果** | 发现附带可运行的 `unittest` | 复现测试 | 几秒 |
| **模型票** | 有 ≥1 张裁判票 | 跨族多数票 | 几分钱 |
| **共识推断** | 没票，但多家提出 | 系统推断（弱证据） | 0 |
| 待定 | 以上都没有 | 进人工队列 | — |

测试断言"正确行为"：当前代码跑红记为「复现」，跑绿记为「未复现」。导入失败、语法错误、超时不算缺陷证据；同一簇执行结果冲突时进人工队列。

**裁判必须与被测分离**，且 `judge_id` 不能含斜杠（否则落盘会写成子目录，`adjudicate` 扫不到，表现为"票收了但票型为 0"）。这一点在实测里踩过。

---

## 计分口径

```
原始分 = Σ(命中 × 严重度权重) − 误报 × 0.5 + 独到命中 × 权重 × 1
终　分 = 原始分 × 时间系数

严重度权重：P0=5　P1=3　P2=2　P3=1
时间系数：超过该题跨候选中位耗时 2 倍开始扣，封顶 5%
```

三个刻意的价值判断（都在 `config/system.toml`，改完重跑 `score` 即生效）：

1. **误报扣 0.5 而不是 1。** 漏掉真 bug 的代价高于多看一个假的。
2. **独到命中权重翻倍。** 只有一家提出的真问题在多数票机制下天然吃亏，需要补偿，否则系统会系统性杀掉最有价值的发现。
3. **报不足 3 条按误报补齐。** 不设这条，模型的最优策略是「一条不报」。

---

## 实测规模

本系统在作者自己的 10 个真实项目上完整跑过一轮（Python / TypeScript / C 混合，5 个项目 3 类技术栈）：

| 指标 | 数值 |
|---|---|
| 参评候选 | 9 家（7 个产品，含同一产品的多个模型） |
| 作答份数 | 90 份（9 × 10 题），解析零错误 |
| 原始发现 | 265 条 |
| 去重后问题 | 124 处（平均每处 2.14 家提出） |
| 裁决结果 | 成立 108、不成立 15、存疑 1 |

这轮跑出来的经验直接改写了本仓库的三处设计，全部写在「已知局限」里——**包括本系统自己的计分缺陷**。建议你把这一节读完再决定信不信任何一张榜单。

---

## 目录

```
src/           四个脚本，零第三方依赖
  cli.py       18 个子命令
  core.py      配置、冻结、时间戳等基础设施
  pipeline.py  归一化 → 聚类 → 裁决 → 计分
  report.py    报告 / CSV / 散点图
prompts/       review_v2（作答）、review_v1（旧版）、judge_v1（裁判）
config/        system.toml（口径）、projects.toml（题库）、candidates.toml（选手）
tasks/<id>/    files.jsonl（冻结的代码）+ prompt.md（拼好的提示词）
runs/<run>/    raw → normalized → claims → judgments → scores.json
reports/       报告、CSV、散点图
docs/          调研笔记、设计说明
docs/method/   13 篇方案文档（需求、架构、数据契约、双盲、裁决、评分、路线图、风险…）
demo/          内置示例
启动.txt        交给 agent 的一键作答指令
第二阶段启动.txt  交给 agent 的一键裁判指令
```

---

## 已知局限（改之前先读）

1. **产品与模型未分离。** 候选写的是「产品/模型」全称，测出来的是组合能力。同一模型在不同产品里表现能差一倍——实测里同一个模型一家报 32 条、一家报 19 条。
2. **可执行裁决只覆盖 Python 标准库 `unittest`。** 前端、C、数据库交互仍靠人工或模型票。
3. **执行目录不是沙箱。** 模型生成的测试必须先审查再授权。
4. **共识是弱证据。** 多家说同一件事，也可能是同一个幻觉模式。
5. **聚类是近似的。** ±8 行 + 标识符交集，仍会把一处问题拆成两条、或把相邻两个问题合成一条。
6. **分数只在本 run 内可比。** 换题、换提示词、换口径就不可横向比较。
7. **单次有噪声。** 每题只跑 1 次时，10% 以内的分差当噪声。
8. **token 记录不完整。** 各家用量展示不一致，「成本」这一轴只作参考。
9. **计分按 claim 累加，同一处问题写多条会重复计分**（`src/pipeline.py` 的计分循环遍历的是 claim 而非 cluster）。实测中第一名因此虚高 33 分、第二名虚高 21 分。**v0.4 计划改为按 `cluster_id` 去重后累加。**
10. **题库里混进构建产物会毁掉一道题。** 实测有一道题冻结的 5 个文件全是 `.next` 产物，9 家候选一致答空，该题零区分度。`exclude` 一定要写 `**/.next/**`、`**/dist/**`、`**/build/**`。
11. **只有一家裁判时，票就是一票定音。** `system.toml` 里 `panel_size = 3`，但实测那轮只跑了一家裁判，且该裁判本身是参评候选之一，其自有发现 100% 被判成立。**至少拉两家非参评模型做裁判**，这比调权重重要得多。
12. **单源成立占比可能很高。** 实测 108 条成立里有 53 条只有一家提出（49.1%）。这部分不是"已验证"，是"未被反驳"。

---

## Roadmap

- [ ] **v0.4**：计分改按 cluster 去重累加；`min_findings` 缺额同步改按处数
- [ ] 裁判多票化：≥2 家非参评模型跑同一批 packets，票型自动转多数票
- [ ] `--api` 采集模式，对支持 API 的候选免去手工粘贴
- [ ] 复现执行支持 JavaScript / C（当前仅 Python `unittest`）
- [ ] 容器化执行环境（Docker + 只读挂载 + 无网络）
- [ ] `scores.csv` 接趋势图，看同一批题在模型版本迭代下的漂移

---

## 文档

`docs/method/` 是完整方案，13 篇、约 3600 行，按顺序读：

| 文档 | 内容 |
|---|---|
| `00_方案总纲.md` | 五个核心设计决定、与现有方案的差异 |
| `01_需求与约束.md` | 要回答什么问题、不能做什么 |
| `02_总体架构.md` | 四层结构、数据流、模块边界 |
| `03_数据契约.md` | 每条发现的字段定义与校验规则 |
| `04_采集作业手册.md` | 怎么跑采集、怎么计时、怎么防作弊 |
| `05_匿名化与双盲.md` | 池化与来源分离、盐哈希、双盲能到哪一层 |
| `06_裁决与共识题库.md` | 四条通道、共识题库、撞库复用 |
| `07_评分规则.md` | 权重推导、双阶段评分、为什么这么定 |
| `08_历史复用与版本.md` | 题库版本、跨轮可比性 |
| `09_实施路线图.md` | 从 P0 到常态运营的分期 |
| `10_风险与局限.md` | 已知失效模式与缓解 |
| `11_运营手册.md` | 真实跑一轮的 SOP |
| `99_其他AI方案对照.md` | 与四份公开方案的逐条对照 |

---

## 许可

MIT。用不用、怎么改都行，但**别把这份榜单当结论**——它测的是"在你的代码上，谁更值得你多看一眼"。
