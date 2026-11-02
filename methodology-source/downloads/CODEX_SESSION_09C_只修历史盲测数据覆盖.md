# Codex Session 09C — 只修历史盲测数据覆盖，不改方法论、不动自动化

背景：Session 09B 返回 `METHODOLOGY_REPLAY_FAIL_DATA_COVERAGE`。
本 Session 只解决 2026-09-04 截止历史盲测的数据覆盖和可重放性，不允许修改方法论标准来迁就数据。

## 一、绝对边界
必须返回：
`METHODOLOGY_CHANGED = NO`
`AUTOMATION_CHANGED = NO`
`PRODUCTION_CHANGED = NO`

严禁修改：
- 收盘/二波方法论
- 潜伏评分与五道门
- Bayesian / Knowledge Star 规则
- Scheduler / Windows Task / ChatGPT automation schedule
- bus/v1 / canonical / hash contract
- IMA adapter / delivery / readback
- Public Feed / APP / private holdings task

## 二、把 DATA_COVERAGE_FAIL 拆开
不得只返回“数据不够”。必须给 2026-09-04 15:00 cutoff 的真实覆盖矩阵：

1. 全市场过去至少20日 OHLCV、涨跌幅、成交额、换手、阶段高点、回撤、缩量程度
2. 行业/板块映射及最近5-10日强弱
3. 相对强度：vs指数、vs板块、弱市抗跌、反弹恢复
4. 3/5/10日资金参与：优先同源；若天然覆盖不足，不能因此让全市场候选生成停摆
5. Knowledge Star / 公司公告 / 财报 / IR / 券商产业语料的候选级覆盖
6. 9/4 市场环境：指数、宽度、成交、实际强方向、风险状态

每项输出：
- universe_count
- covered_count
- coverage_rate
- source
- missing_reason
- remediation

## 三、两阶段数据获取，不改模型标准

### Stage 1：全市场粗筛
只用覆盖最稳定的数据：
- 20日 OHLCV
- 第一波存在
- 2-8日回调/横盘
- 回撤幅度
- 缩量程度
- 关键结构是否破坏
- 基础相对强度
- 板块没有明显死亡

目标：全市场缩到 Top 100-300 个结构候选。
Stage 1 不是最终潜伏池。

### Stage 2：证据富集
只对 Stage 1 候选补：
- 3/5/10日资金参与
- Knowledge Star 历史与当时增量
- 公司公告/财报/IR
- 产业逻辑
- 板块核心关系

最后才计算完整五维评分：
- 第一波质量20
- 回调质量25
- 资金参与25
- 语料与逻辑20
- 板块环境10

这只是执行顺序优化，不改变方法论。

## 四、缺失数据规则

### HARD_REQUIRED
缺失以下任一项，该股票不能进入正式“重点潜伏”：
- 基本 OHLCV
- 第一波是否存在
- 当前回调结构
- 关键结构是否破坏

### ENRICHMENT_REQUIRED_FOR_FINAL_SCORE
以下缺失时可以进候选，但不能直接升级“重点潜伏”：
- 资金参与证据不足
- Knowledge Star/公司语料不足
- 板块相对强弱不足

此时状态只能是：
`DATA_INCOMPLETE_RESEARCH`

客户前台以后翻译为：
“形态进入观察，但资金/产业证据还不完整，暂不升级。”

不得因为数据缺失直接给0分淘汰，也不得凭空补分。

## 五、数据源优先级
优先复用已有：
1. tdx / westock / Wind
2. repo 已有历史行情 / screener / K-line 工具
3. Knowledge Star collector / cache
4. 公告/财报/IR source
5. 可靠公开历史数据接口，仅用于 replay 补齐

禁止为一次 replay 新建大型数据库，禁止改生产 collector，禁止改每日 Scheduler。
历史 replay 数据只能放独立临时目录/test fixture。

## 六、先生成 Coverage Matrix
必须生成：
- `replay-data-coverage-20260904.json`
- `replay-data-coverage-20260904.md`

至少有：
dimension / required / source / universe covered / coverage / blocker / remediation

## 七、补齐后立即重跑 9/4 blind replay
只有当：
- 全市场 OHLCV hard coverage 足以运行 Stage 1
- Stage 2 至少能对 Top candidates 做资金 + 语料富集

才重跑。
严格 cutoff：
`2026-09-04 15:00 Asia/Shanghai`
禁止任何 cutoff 后数据。

## 八、重跑必须返回
1. Stage 1 candidate count
2. Stage 2 enriched count
3. Top20 最终候选
4. 每只五维评分
5. 合锻智能诊断
6. 福立旺诊断
7. 长城科技诊断
8. 三只全市场排名
9. false-positive review
10. false-negative review
11. 纯题材脉冲污染率
12. 板块已死亡污染率
13. 高位未回调污染率
14. 数据不完整候选数量

三个诊断样本不得硬编码。

## 九、如果仍不能补齐
只能返回：
- `REPLAY_DATA_HARD_BLOCKED_<specific_reason>`
或
- `REPLAY_DATA_ENRICHMENT_PARTIAL`

若 OHLCV 全市场可跑、但资金/语料部分不足：
必须继续运行 Stage 1，并对可富集候选跑 Stage 2，不能整轮停止。

## 十、最终返回
A. DATA COVERAGE MATRIX
B. HARD REQUIRED COVERAGE
C. ENRICHMENT COVERAGE
D. DATA SOURCES USED
E. TEMP DATA CREATED
F. METHODOLOGY_CHANGED = MUST BE NO
G. AUTOMATION_CHANGED = MUST BE NO
H. PRODUCTION_CHANGED = MUST BE NO
I. REPLAY RERUN STATUS
J. TOP20 / SAMPLE DIAGNOSTICS（若可跑）
K. REMAINING BLOCKERS
L. CLEANUP / ROLLBACK

最终状态只能是：
- `REPLAY_DATA_READY_AND_METHODOLOGY_REPLAY_COMPLETE`
- `REPLAY_DATA_ENRICHMENT_PARTIAL`
- `REPLAY_DATA_HARD_BLOCKED_<specific_reason>`
