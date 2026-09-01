# Development guide

## 常见任务入口

| 任务 | 首选位置 | 修改后至少验证 |
|---|---|---|
| 新增或调整数据源 | `data_provider/providers/`、`data_provider/scheduler.py` | Provider 健康状态、空数据降级、缓存/Raw 边界 |
| 修改数据库合同 | `foundf_db/schema.py`、`foundf_db/migration.py` | 新旧库迁移、幂等、只读兼容 |
| 修改因子 | `factor_engine/`、`research_engine/` | PIT 对齐、IC/衰减/稳定性、缺失值行为 |
| 修改候选策略 | `quant_strategy/` | 数据新鲜度、决策时点、版本与证据哈希 |
| 修改回测 | `backtest_engine/v2/` | 无未来数据、T+1 执行、成本、基准和失败门禁 |
| 修改组合或风险 | `portfolio_manager/`、`risk_engine/` | 币种、估值日期、集中度、缺失数据 fail-closed |
| 修改 Dashboard | `api/` | API 失败时清空旧值、只读边界、认证配置 |
| 修改模拟手机流程 | `deploy/phone/` | 模拟标识、设备钉定、页面不匹配时中止 |

## 本地环境

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` 仅供本机使用，已被 Git 忽略。数据源凭据见 `DATA_SOURCE_SETUP.md`。

## 设计约定

- 时间字段应区分生成时间、数据截止时间、披露时间和执行时间。
- 数据层保存来源和原始证据；研究层不得覆盖原始层。
- 金额计算必须显式携带币种和汇率口径。
- 对外 API 只投影必要字段，不能返回 Raw 券商资料或凭据。
- 自动任务应幂等，重跑不能制造重复记录或重复动作。
- 外部服务不可用时返回可解释状态，不使用伪造样本保持流程“成功”。

## 提交前检查

```bash
git status --short
git diff --check
docker compose config --quiet
git diff -- . ':!requirements-lock.txt'
```

另外搜索即将提交的文件，确认不存在 Token、密码、私有 IP、绝对主机路径、设备序列号、账户尾号和真实交易数据。
