# Codex Session 09D — 接现有 a-stock-data，补二波候选证据，不做研究判断

## 背景
Session 09C 已完成 Stage 1：
- 全市场严格 OHLCV 20 日覆盖：5,176 / 6,134
- Stage 1 结构候选：199
- 但 Stage 2：同源资金 0/199、行业/板块 0/199、严格 Knowledge Star 0/199、公告/财报/IR 0/199

当前问题不是方法论，而是没有接上项目既有正式数据 Skill。

## 一、唯一正式数据 Skill
必须首先读取并遵守：

`.agents/skills/a-stock-data/SKILL.md`

正式版本：`a-stock-data v3.4.0`

如存在用户级安装，可核对：
`C:\Users\15707\.codex\skills\a-stock-data\SKILL.md`

禁止把 `.agents/skills/a-stock-data-backup-legacy` 当正式源。

批准的数据路线：
`local preflight → official a-stock-data v3.4.0 → local adapters → local K-line cache → v41-data-source-fallback → data_source_health → evidence`

优先使用：
`D:\向日葵\alphaengine\resources\python\python\python.exe`

## 二、角色边界
Codex 只负责：
- 找数据
- 调接口
- 算机械特征
- 做历史 cutoff
- 打包候选 evidence
- 标记数据质量

Codex 不得负责：
- 判断产业逻辑是否值得买
- 最终机会信用
- 决定“重点潜伏”
- 主观给语料打分
- 生成最终 Bayesian posterior
- 写最终客户收盘/二波文章

最终研究判断属于 ChatGPT Brain。

## 三、严格历史截止
截止：
`2026-09-04 15:00 Asia/Shanghai`

任何 evidence 必须满足：
`published_at / trade_date / effective_date <= cutoff`

禁止使用 9/7 及之后的行情、资金、Knowledge Star、公告/IR、后续市场表现。

行业分类若只能取得当前标签：
- 只能做 metadata
- 必须标记 `historical_membership_verified=false`
- 不得用当前概念归属反向解释 9/4

## 四、只处理 Stage 1 的 199 个候选
不要重新造一套全市场模型。
输入就是 Session 09C 的 199 个 Stage 1 候选。
目标只做 Stage 2 证据富集。

## 五、必须补齐的字段

### A. 行情/交易状态
尽量补：
- 20日 OHLCV
- 成交额
- 换手率
- 量比（历史可计算则算）
- 阶段高点
- 回撤
- 第一波高量区
- 回调期量能
- up/down volume asymmetry
- 相对上证强度
- 相对行业/板块强度（若可得）

上轮成交额、换手率为 0 覆盖，这次必须优先补。

### B. 资金参与
按 skill fallback：
优先 EastMoney push2/push2his；失败后切 THS、read-only akshare、现有本地缓存/历史 recap artifacts。

形成：
- 3/5/10 日逐日资金序列
- 累计净流入/流出
- 正流入日数量
- 回调时是否仍有资金净流入
- 下跌缩量/上涨放量的参与不对称

硬规则：
- 同一接口失败 2 次必须切 fallback
- 不得 199 个候选都停在同一个 RemoteDisconnected
- 不得把单日主力净流入解释为吸筹
- 只输出机械事实

最终仍缺失则：
`fund_evidence_status = missing`

### C. 行业/板块
使用：
- a-stock-data 行业/概念能力
- THS hot reason/theme tags
- repo 现有 THS/Sina 历史横截面快照
- 既有 sector/industry adapters

每只候选至少输出：
- stable_industry
- theme_tags[]
- historical_membership_verified
- sector_5d_return（如可得）
- sector_10d_return（如可得）
- stock_vs_sector_5d
- sector_breadth/core_count（如可得）

### D. Knowledge Star / IMA / 历史研究
截至 9/4 搜：
1. Knowledge Star collector/cache
2. `zsxq_scan_20260904.json`
3. `evidence_index.json`
4. 既有历史 recap/research artifacts
5. IMA 截止 9/4 的历史研究材料（若现有检索可用）
6. 公司公告/财报/IR

每条只输出：
- source_type
- published_at
- title
- summary_or_excerpt
- evidence_ref
- source_reliability
- cutoff_valid

Codex 只做机械去重，不判断 reinforce/weaken/falsify/extend。
这些由 Brain 判断。

### E. 公告 / 财报 / IR
按 a-stock-data 既有路径：
- announcement arrays
- stock news
- official/public news
- F10 latest prompt
- CNINFO/EastMoney
- Hudongyi/e互动
- IR/research notes

只取 `<= 2026-09-04 15:00`

输出：
- latest_material_disclosures[]
- latest_financial_report
- investor_relations[]
- major_contract/order
- reduction/buyback/placement/restructuring flags

缺失必须区分：
- endpoint_not_called
- endpoint_failed
- successful_zero_row
- no_historical_coverage

## 六、运行 data source health
复用既有 skill：
`scripts/data_source_health.py 20260904 --root D:\向日葵\alphaengine`
`scripts/augment_breadth_emotion_evidence.py 20260904 --root D:\向日葵\alphaengine`

若脚本只支持当日、不支持 replay：
- 不改生产脚本
- 只在独立 replay adapter/fixture 复用 endpoint/fallback 逻辑
- 标记历史不可复现字段

## 七、输出 Brain Packet，不做最终评分
输出：
`brain-packets/2026-09-04-second-wave-lurking-replay-evidence.json`
`brain-packets/2026-09-04-second-wave-lurking-replay-evidence.md`

每只候选至少包含：
- structure_features
- fund_features
- sector_features
- knowledge_evidence
- announcement_ir_evidence
- data_quality
- mechanical_flags

禁止输出：
- final_score
- final_rank_by_research
- 重点潜伏
- 买入/卖出
- posterior
- 最终机会信用

可以输出：
- mechanical_structure_score
- data coverage
- raw evidence counts
- missing fields

## 八、三个诊断样本
无论是否在 Stage 1，都单独生成 diagnostic：
- 合锻智能
- 福立旺
- 长城科技

如果福立旺/长城科技未进入 Stage 1：
- 不得人为加回 candidate universe
- 只回放其结构特征
- 明确是哪条机械 Stage 1 规则挡掉
- 给 Brain 原始数据判断该规则是否过严

## 九、新 coverage 必须返回
至少：
- OHLCV
- turnover amount
- turnover rate
- relative index
- stable industry
- historical sector strength
- 3/5/10d fund
- Knowledge Star evidence
- announcement
- IR
- market environment

## 十、成功标准
1. 199 条 Stage 1 不丢
2. a-stock-data 正式路线实际被调用
3. EastMoney 失败后确实按 skill fallback
4. 至少显著提高以下任意三项：资金、行业/板块、成交额/换手、Knowledge Star/历史研究、公告/IR
5. 产出完整 Brain Packet
6. 不做研究判断
7. 不改方法论和生产链

## 十一、严格禁止修改
- 方法论冻结稿
- 潜伏五道门
- 评分规则
- 盘前/收盘/private Brain prompt
- Scheduler / Windows Task
- bus/v1 / canonical
- IMA / Public Feed / APP
- production collector

## 十二、最终返回
A. A_STOCK_DATA_SKILL_FOUND
B. SKILL_VERSION
C. OFFICIAL_ROUTE_USED
D. FALLBACKS_USED
E. COVERAGE_BEFORE → AFTER
F. 199 CANDIDATE EVIDENCE PACKET
G. 合锻智能 DIAGNOSTIC
H. 福立旺 DIAGNOSTIC
I. 长城科技 DIAGNOSTIC
J. REMAINING MISSING DATA
K. METHODOLOGY_CHANGED = NO
L. AUTOMATION_CHANGED = NO
M. PRODUCTION_CHANGED = NO
N. FILES_CREATED
O. CLEANUP / ROLLBACK

最终状态：
`SECOND_WAVE_REPLAY_EVIDENCE_PACKET_READY`
或
`SECOND_WAVE_REPLAY_EVIDENCE_PACKET_PARTIAL_<specific_reason>`
