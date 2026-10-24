# SECS Vision web system: documents / 网页系统文档

The web system (`webapp/`) turns the recognition results of `alpr/` into a searchable, alerting, forecasting
system for traffic police, guardhouse security and traffic managers.
网页系统（`webapp/`）把 `alpr/` 的识别结果变成可检索、会告警、能预测拥堵的系统，服务交警、门岗保安和交通管理人员。

| Document / 文档 | English | 中文 |
|---|---|---|
| Requirements specification: users, roles, use cases, FR / NFR with their tests, limitations / 需求规格说明：用户、角色、用例、功能与非功能需求及其测试、局限 | [REQUIREMENTS_EN.md](REQUIREMENTS_EN.md) | [REQUIREMENTS_ZH.md](REQUIREMENTS_ZH.md) |
| Architecture and page layout (as built) / 架构与页面布局（已实现） | [ARCHITECTURE_EN.md](ARCHITECTURE_EN.md) | [ARCHITECTURE_ZH.md](ARCHITECTURE_ZH.md) |
| Database: every table, natural keys, audit chain, retention / 数据库：各表、自然键、审计哈希链、保留期限 | [DATABASE_EN.md](DATABASE_EN.md) | [DATABASE_ZH.md](DATABASE_ZH.md) |
| API overview and the service functions for the AI assistant / API 概览及供 AI 助手调用的服务函数 | [API_EN.md](API_EN.md) | [API_ZH.md](API_ZH.md) |
| Test plan and report / 测试计划与报告 | [TEST_PLAN_REPORT_EN.md](TEST_PLAN_REPORT_EN.md) | [TEST_PLAN_REPORT_ZH.md](TEST_PLAN_REPORT_ZH.md) |
| Demo walkthrough with screenshots (three defence scenarios) / 演示走查及截图（三个答辩场景） | [DEMO_WALKTHROUGH_EN.md](DEMO_WALKTHROUGH_EN.md) | [DEMO_WALKTHROUGH_ZH.md](DEMO_WALKTHROUGH_ZH.md) |

## Quick start / 快速开始

```bash
.venv\Scripts\python -m webapp.importer --demo --demo-users
```

```bash
.venv\Scripts\python -m webapp
```

Then open <http://127.0.0.1:8000> (API reference at `/docs`). Demo accounts: `webapp/seed/demo_users.yaml`, local
demo only. Tests: `python alpr/tests/test_alpr.py` and `python webapp/tests/test_web.py` (both run in CI on every
push).
然后打开 <http://127.0.0.1:8000>（API 参考在 `/docs`）。演示账号见 `webapp/seed/demo_users.yaml`，只用于本地演示。
测试：`python alpr/tests/test_alpr.py` 和 `python webapp/tests/test_web.py`（每次推送都在 CI 中运行）。

The vehicle registry is SIMULATED and every violator is a fictitious X-series plate; see the requirements, section 7.
车辆登记库是模拟的，所有违规车辆都是虚构的 X 开头车牌；见需求规格说明第 7 节。
