# 研盛日常运行交接

核对时间：2026-11-02，北京时间。本文记录现有入口与可复核证据，带尖括号的路径是每次运行需要替换的参数，不是已执行命令。实际研究由 WorkBuddy 完成；采集和程序校验不能代替阅读和编辑审阅。

## 时间与执行入口

用户转述的 WorkBuddy 任务配置为：周日—周四 21:30 夜间准备；周一—周五 06:40 盘前、15:10 收盘，均用官方交易日历门控；盘前 07:15、收盘 15:40 补查。周五、周六晚上不安排夜间准备。任务状态及 ID 来自 2026-10-31 WorkBuddy 的回报；本次读取到的 `C:/Users/15707/AppData/Roaming/WorkBuddy/automations/automations.db` 没有任务行，尚不能据此独立确认该回报对应的当前调度器，也不能据此断言任务不存在。11 月 1 日夜间任务有执行记录，但当晚电脑开机时间晚于计划时间。真实触发与目标时间分别核对。

日常工作目录：`C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine`。入口为该目录的 `.agents/skills/yansheng-daily-production/SKILL.md`，按它读取 `references/workflow.md`、`preparation.md`、`reading-reliability.md`、`public-hosting.md` 及指定方法文件。运行产物分别记录 preflight、sources、research、draft、review、build、publish。网站发布是唯一日常交付；IMA 不写入。

## 夜间与交易日门控

```powershell
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/night_preparation.py' gate
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/calendar_gate.py' --session premarket
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/calendar_gate.py' --session close
```

自动任务不传历史 `--date`。门控脚本通过 `daily-recap-trading/scripts/production/trading_context_v1.py` 读取 `D:/向日葵/alphaengine/calendar/published/current.json`；`SKIP_NON_TRADING_DAY` 结束，`BLOCKED_CALENDAR` 是日历缺口。夜间准备保存资料及交接，不生成最终正文或发布。

```powershell
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/night_preparation.py' seal --plan '<prepared-plan.json>' --output '<revision>/preparation-manifest.json'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/night_preparation.py' check --output '<run>/preparation-check.json'
```

## 数据来源与读取

知识星球文字正文来自两组：短评&信息 `15552822254242`、浑水调研 Plus `28512858211281`。下面是 `reading-coverage-contract.md` 记录的采集命令；采集不表示已阅读：

```powershell
& 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/capture_zsxq_window.ps1' -Start '<上交易日15:00，含时区>' -OutputDir '<私有快照目录>'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/reading_audit.py' --enumeration '<enumeration.json>' --ledger '<reading-ledger.json>' --coverage '<source-coverage.json>' --output '<run>/zsxq-coverage-audit.json'
```

日常行情工具是 [a-stock-data](https://github.com/simonlin1212/a-stock-data)，本机文件为 `C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/a-stock-data/SKILL.md`。它是含各数据接口示例代码的技能文件，不是一个统一的命令行程序。按数据类型读取其中相应函数与依赖，再用 Python 执行所需示例；文件列出的基础依赖安装命令为 `pip install mootdx requests pandas stockstats`。本机技能标记 v3.6.1，远端仓库可能更新。行情可用通达信 mootdx、腾讯等，公告可查巨潮、交易所；星球与海外资料由各自来源读取。是否可用、字段与时间需按本次实际响应核对。

独立采集入口和研究登记命令：

```powershell
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/collection_only.py' --session '<premarket或close>'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/workflow.py' init --run '<run目录>' --date '<YYYY-MM-DD>' --session '<premarket或close>'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/workflow.py' status --run '<run目录>'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/workflow.py' record --run '<run目录>' --stage '<阶段>' --files '<该阶段实际产物>'
```

`collection_only.py` 调用 `bus_v1.collect`；上面的采集入口不代表已核实另有独立 06:30 或 15:05 系统定时任务。WorkBuddy 基于真实资料完成人工式研究、正文与内容审阅，脚本只负责对应工具操作和记录。

## 导出与发布

导出器实际位置 `D:/向日葵/yansheng-public-preview/export_research.py`；它使用同目录 `public/today.html`、`workbench.py`、`opportunity_view.py` 和本地历史数据生成 `out/`。以下参数依据脚本 `--help` 和现行流程整理，不是某次运行的逐字命令：

```powershell
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/daily-recap-trading/scripts/production/research_brief.py' --date '<YYYY-MM-DD>' --session '<premarket或close>' --report '<正文.md>' --analysis '<research-brief.json>'
python 'D:/向日葵/yansheng-public-preview/export_research.py' --report '<正文.md>' --date '<YYYY-MM-DD>' --session '<premarket或close>' --identity '<正式版或审阅版>' --analysis '<research-brief.json>' --workflow '<run>/workflow-state.json' --receipt '<导出回执.json>'
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/publish_github_pages.py' --source 'D:/向日葵/yansheng-public-preview/out' --workflow '<run>/workflow-state.json' --checkout 'D:/向日葵/yansheng-public-github' --repo 'huanghongwei449-droid/yansheng-research-web' --receipt '<run>/web-deployment-receipt.json' --publish
python 'C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/yansheng-daily-production/scripts/workflow.py' record --run '<run目录>' --stage publish --files '<run>/web-deployment-receipt.json'
```

`--pool`、`--connections`、`--checker` 等按导出器帮助和本次产物实际情况指定。发布器验证同稿、推送公开仓库 `main`、等待 Pages 构建并匿名读回；回执保存在运行目录。访问地址：https://huanghongwei449-droid.github.io/yansheng-research-web/today.html 。

2026-11-02 复查时，线上 `publication.json` 显示盘前 2026-11-02、收盘 2026-10-30；本机盘前回执 `status=succeeded`，Pages commit `180fd41fb20043c252c37d0e2e20253afbab251c`，GitHub 构建状态 `built`。这证明该次回执及公开清单，不证明每种访客网络均可访问或内容已被用户认可。
