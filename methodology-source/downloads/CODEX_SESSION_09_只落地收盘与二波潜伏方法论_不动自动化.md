# Codex Session 09 — 只落地收盘/二波潜伏方法论，不动自动化

## 前置条件
- Session 08 客户呈现层如正在执行，先完成或保证无文件冲突。
- 本 Session 只做研究方法论代码/skill/规则落地与历史回放。
- 不修改任何生产 Scheduler、Windows Task、ChatGPT automation schedule、bus/v1、canonical、IMA delivery/readback、Public Feed、APP。

## 唯一方法论来源
读取并以此为准：

`研枢_收盘复盘与二波潜伏模型_方法论冻结稿_20260908.md`

不得自行扩展为另一套二波模型。

---

# A. 先审计当前方法论实现

找出现有：
- close review skill / prompt / renderer
- second-wave / opportunity pool skill
- market discovery
- fund / relative-strength / kline scan
- Knowledge Star evidence integration
- opportunity lifecycle / credit state
- GBrain / Bayesian hooks

输出映射：
- 当前规则
- 冻结稿对应规则
- 缺失
- 冲突
- 需要保留

重点检查旧模型是否把 `market_reselection_evidence` 当成进入候选池的硬前提。

---

# B. 收盘模块只负责“判市场”

按冻结稿落实：

facts-first
→ blind scan
→ competing explanations
→ premarket grading
→ misses / mapping divergence
→ knowledge sedimentation
→ Bayesian update
→ risk budget
→ 2—3 next-day priors

收盘模块输出 standardized market context 给机会模型。

不得让收盘模块为了凑机会而选股。

---

# C. 二波模型增加独立“重点潜伏”阶段

保留旧纪律：
- direction strong ≠ old stock credit restored
- no reselection ≠ second wave confirmed
- thesis intact ≠ trade opportunity
- dynamic upgrade/downgrade/remove

新增：

```text
研究雷达
→ 潜伏观察
→ 重点潜伏
→ 启动确认
→ 二波进行
→ 降级/剔除
```

`重点潜伏` **不要求已经出现 market reselection evidence**。

但必须同时有：

1. 第一波已被市场选择
2. 2—8日良性回调/横盘
3. 缩量、关键结构未破
4. 3—10日仍有资金/承接证据
5. Knowledge Star / 公司 / 产业语料仍支持
6. 板块没有明显死亡

---

# D. 资金判断不能偷懒

禁止：
- 单一“主力净流入” = 有主力吸筹
- 跌得少 = 有资金潜伏

至少多维共振。

实现可用指标候选：

- down-volume contraction
- up/down volume asymmetry
- relative strength vs index/sector
- drawdown from first-wave high
- turnover persistence
- price acceptance around first-wave cost/breakout zone
- same-source 3/5/10d flow sequence（仅辅助）
- strong-market / weak-market behavior

如数据源不足，输出 missing，不伪造。

---

# E. 语料接入

候选必须关联：

- company announcement / financial / IR
- Knowledge Star historical topic
- Knowledge Star daily increment
- industry / broker research where available

每条判断：
- new
- reinforce
- weaken
- falsify
- duplicate
- unresolved

不要把热帖数量当 score。

---

# F. 潜伏评分

按冻结稿：

- 第一波质量 20
- 回调质量 25
- 资金参与 25
- 语料与逻辑 20
- 板块环境 10

输出总分和逐项证据。

参考等级：
- >=75 重点潜伏
- 65—74 潜伏观察
- 55—64 研究
- <55 不进池

重大证伪可以直接覆盖分数。

---

# G. 当日大涨股票的处理

如果候选今天已经大幅启动：
- 不再叫潜伏
- 自动进入“启动确认 / 二波进行”
- 不在下一日潜伏低吸清单里继续作为同类推荐

禁止大涨后倒推“昨天就应该买”。

---

# H. 关键验收：2026-09-04 历史盲测

冻结 cutoff：

`2026-09-04 15:00 Asia/Shanghai`

严禁使用：
- 2026-09-07 行情
- 2026-09-07 资金
- 2026-09-07 Knowledge Star
- 任何 9/4 cutoff 后的信息

运行全市场潜伏扫描。

重点生成 diagnostic，对以下三个样本仅做方法论检验：

- 合锻智能
- 福立旺
- 长城科技

注意：
不是硬编码“必须选中”。
要回答：

1. 截止 9/4，它是否有第一波？
2. 是否处于缩量回调/横盘？
3. 资金参与证据有哪些？
4. 历史/当时语料有哪些？
5. 板块是否死亡？
6. 五维评分多少？
7. 当时应处于：
   - 研究
   - 潜伏观察
   - 重点潜伏
   - 不应入池
8. 如果没进入重点潜伏，具体哪条规则挡住？
9. 同样规则会额外筛出多少垃圾票？

同时返回全市场 top candidates，不得只评三个已知样本。

---

# I. 防过拟合验收

必须给出：

- top 20 候选
- 三个样本在全市场排名
- false-positive review
- 是否大量出现纯题材脉冲票
- 是否大量出现板块已死亡票
- 是否大量出现高位尚未调整票
- 是否出现“主力净流入”单字段驱动的误判

如果三个样本靠人工特判才能进池，FAIL。

---

# J. 客户输出语言

遵守 Session 08 presentation policy。

客户版只讲：
- 为什么曾经强
- 为什么回调质量好
- 为什么资金可能没走完
- 产业/语料还有什么
- 明天什么条件动
- 什么条件放弃

不出现：
prior/posterior、market_reselection_evidence、second_wave_return、incubation、raw enum、工程字段。

---

# K. 严格禁止修改

- Scheduler
- Windows Task
- ChatGPT automation schedule
- ChatGPT active task prompt（本 Session 不改）
- bus/v1
- canonical
- hash contract
- IMA adapter
- delivery/readback
- Public Feed
- APP
- private holdings task
- origin/main reconciliation

本 Session 结束后，只返回“方法论是否可上线”。
真正接入下午生产任务，另开独立步骤。

---

# L. 返回

A. CURRENT METHOD AUDIT
B. CLOSE METHOD CHANGES
C. SECOND-WAVE METHOD CHANGES
D. LURKING SCAN IMPLEMENTATION
E. FUND SIGNAL DESIGN
F. KNOWLEDGE INTEGRATION
G. 2026-09-04 BLIND REPLAY
H. SAMPLE DIAGNOSTICS
I. FULL-MARKET TOP CANDIDATES
J. FALSE POSITIVE REVIEW
K. TESTS
L. FILES CHANGED
M. AUTOMATION_CHANGED = MUST BE NO
N. PRODUCTION_CHANGED = MUST BE NO
O. ROLLBACK

最终：
`METHODOLOGY_READY_FOR_PRODUCTION_MAPPING`
或
`METHODOLOGY_REPLAY_FAIL_<reason>`
