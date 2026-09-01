# Data source credentials and licensing

本仓库不提供任何数据源 Token、API Key、账号、Cookie 或授权文件。每位使用者必须自行向数据提供方申请，并遵守其许可、频率、存储和再分发条款。

## 配置步骤

```bash
cp .env.example .env
chmod 600 .env
```

只在本地 `.env` 填写真实值。不要把 Token 写进 Python、JSON 示例、命令参数、日志、截图、Issue 或聊天记录；不要提交 `.env`。

## 支持的数据源

| 数据源 | 本地变量/配置 | 使用者需要做什么 |
|---|---|---|
| Tushare Pro | `TUSHARE_TOKEN` | 在 Tushare 官方平台自行注册、申请 Token，并确认当前积分与接口配额 |
| FRED | `FRED_API_KEY` | 在 FRED 官方开发者页面自行申请 API Key |
| SEC EDGAR | `SEC_USER_AGENT` | 填写可识别的应用名和自己的联系邮箱；它不是密码，但不应沿用示例地址 |
| Brave Search | `BRAVE_SEARCH_API_KEY` | 自行开通官方 API，并确认订阅是否允许保存搜索结果 |
| Google Programmable Search | `GOOGLE_CSE_API_KEY`、`GOOGLE_CSE_ID` | 仅在自己已有并可合法使用的项目中配置 |
| Bloomberg | `BLOOMBERG_*` | 需要有效的 Bloomberg 授权和本地 BLPAPI；不得绕过 entitlement |
| Investing.com 导出 | `INVESTING_EXPORT_LICENSE_CONFIRMED` | 仅导入使用者有权保存的手工导出文件，不使用本仓库抓取网站 |
| baostock / yfinance / HKEX | 通常无 Token | 仍需检查当前服务条款、频率和数据口径；无 Token 不等于可无限抓取或再分发 |

## 示例

```dotenv
TUSHARE_TOKEN=
FRED_API_KEY=
SEC_USER_AGENT=
BRAVE_SEARCH_API_KEY=
GOOGLE_CSE_API_KEY=
GOOGLE_CSE_ID=
```

空值表示功能未配置。代码应报告 `UNAVAILABLE`、`DEGRADED` 或跳过，而不是使用内置共享凭据。

## 提交前自检

```bash
git diff --cached --name-only
git diff --cached
```

若 Token 曾进入 Git，即使随后删除，也应立即在数据提供方后台撤销并重新生成，因为旧值仍可能存在于 Git 历史中。
