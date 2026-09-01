# FoundF agent guide

本文件供 Kimi、Codex 和其他代码代理在脱敏仓库中接手开发。它只描述公开框架，不代表任何真实账户或生产环境授权。

## 开始前

依次阅读：

1. `README.md`
2. `PUBLIC_RELEASE.md`
3. `docs/PROJECT_VISION.md`
4. `docs/DEVELOPMENT.md`
5. 涉及数据源时阅读 `docs/DATA_SOURCE_SETUP.md`

先执行 `git status --short`，保留已有修改，不使用破坏性 Git 命令。

## 架构入口

```text
foundf_db
  -> data_provider
  -> factor_engine / quant_strategy / risk_engine
  -> portfolio_manager
  -> portfolio_ai / strategy_manager
```

- 数据合同、Schema、迁移：`foundf_db/`
- 数据源与调度：`data_provider/`
- 因子研究与候选：`factor_engine/`、`research_engine/`、`quant_strategy/`
- 组合、估值与风控：`portfolio_manager/`、`risk_engine/`
- 策略治理和 Walk-Forward：`strategy_manager/`、`backtest_engine/v2/`
- 只读 API/Dashboard：`api/`
- 模拟手机操作：`deploy/phone/`

## 强制边界

- 不提交 `.env`、Token、API Key、密码、Cookie、设备序列号或本机地址。
- 不提交 `data/`、`reports/`、截图、UI XML、日志、持仓、成交或券商导出。
- 需要 Token 的数据源由使用者从官方渠道自行申请，并仅写入本地环境；不要编造或共享 Token。
- 不添加真实券商登录、账户抓取或实盘下单功能。本仓库的手机执行范围仅限明确标识的模拟环境。
- 缺失、陈旧、混币种或无法对账的数据必须 fail-closed，不得补零或猜值。
- 新因子必须有定义、数据来源、样本外证据和稳定性验证，不能只优化历史最高收益。

## 修改流程

1. 明确修改提升了数据可靠性、组合分析、风险控制或长期策略稳定性中的哪一项。
2. 找到单一权威模块，避免在多个回测/策略实现中重复添加逻辑。
3. 保持输入时点安全、错误降级和审计字段。
4. 更新相邻文档和示例配置；示例值只能是空值或明显占位符。
5. 运行与改动相称的验证，并在交付中报告真实结果。

## 最低验证

```bash
python3 - <<'PY'
import ast
from pathlib import Path
for path in Path('.').rglob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
print('Python syntax OK')
PY

find . -type f -name '*.sh' -print0 | xargs -0 -r -n1 bash -n
docker compose config --quiet
git diff --check
```

若新增测试，再运行对应的 `pytest`；涉及 Schema、调度、回测或风控时应补充针对性测试。

## 与 ctlphone 联调

FoundF 通过 `deploy/phone/phone_client.py` 使用 ctlphone。设置：

```bash
export PHONE_CTL_HOME=/path/to/ctlphone
export FOUNDF_ADB_SERIAL=your-adb-device-serial
```

不得把路径或序列号写回仓库。任何真机操作前都要确认设备属于操作者且已明确授权。
