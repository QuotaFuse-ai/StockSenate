# 外部链接清单

本文件汇总外部资源、依赖规范与项目入口，供验收项 19 逐项核验。新增外部链接时请同步追加到本文件；`scripts/check_links.py` 会逐个请求并报告失效链接。

```bash
python scripts/check_links.py                 # 联网核验
python scripts/check_links.py --offline       # 离线跳过（等价于 STOCKSENATE_SKIP_LINK_CHECK=1）
```

## 项目入口

| 名称 | 链接 | 说明 |
| --- | --- | --- |
| 项目主页 / 仓库 | https://github.com/QuotaFuse-ai/StockSenate | 独立官网尚未上线，暂以仓库主页作为官网入口 |
| Issues | https://github.com/QuotaFuse-ai/StockSenate/issues | 缺陷与需求跟踪 |
| Releases | https://github.com/QuotaFuse-ai/StockSenate/releases | 版本标签与构建产物 |
| PyPI 项目页 | https://pypi.org/project/stocksenate/ | 发行包入口（0.1.0 起，wheel + sdist） |

## 研究语境与许可证

| 名称 | 链接 | 说明 |
| --- | --- | --- |
| TradingAgents 论文 | https://arxiv.org/abs/2412.20138 | 多角色投研框架的方法来源 |
| TauricResearch / TradingAgents | https://github.com/TauricResearch/TradingAgents | 多智能体投研框架（Apache-2.0），本项目术语与议题的来源 |
| simonlin1212 / TradingAgents-astock | https://github.com/simonlin1212/TradingAgents-astock | A 股场景的同类公开实现（Apache-2.0） |
| Apache License 2.0 | https://www.apache.org/licenses/LICENSE-2.0 | 本项目许可证全文 |
| SPDX: Apache-2.0 | https://spdx.org/licenses/Apache-2.0.html | 许可证标识符定义 |

许可证：本项目采用 Apache-2.0；第三方归属与许可证声明见仓库根目录 `NOTICE`。

## 规范与标准

| 名称 | 链接 | 说明 |
| --- | --- | --- |
| PEP 517 | https://peps.python.org/pep-0517/ | 构建后端接口（`setuptools.build_meta`） |
| PEP 621 | https://peps.python.org/pep-0621/ | `pyproject.toml` 项目元数据 |
| Semantic Versioning 2.0.0 | https://semver.org/ | 版本号语义 |
| Keep a Changelog 1.1.0 | https://keepachangelog.com/en/1.1.0/ | 变更记录格式 |

## 运行时依赖官方文档

| 依赖 | 链接 |
| --- | --- |
| Typer | https://typer.tiangolo.com/ |
| Click | https://click.palletsprojects.com/ |
| Rich | https://rich.readthedocs.io/ |
| Pydantic | https://docs.pydantic.dev/ |
| Starlette | https://starlette.dev/ |
| Uvicorn | https://uvicorn.dev/ |
| Model Context Protocol | https://modelcontextprotocol.io/specification/ |
| OpenAI API | https://platform.openai.com/docs/ |
| Anthropic Python SDK | https://github.com/anthropics/anthropic-sdk-python |

Anthropic 官方 API 文档站点按地区开放，这里链接到官方 Python SDK 仓库，保证在受限网络下也可核验。
| uv | https://docs.astral.sh/uv/ |
| pytest | https://docs.pytest.org/ |
| Docker Compose | https://docs.docker.com/compose/ |

## 数据底座

TickHub-CN 与 TickHub-US 由部署环境提供（`tickhub-cn>=1.0`），属于私有底座，没有公开链接；端口与边界见 [DEC-003](../decisions/DEC-003-service-topology-and-ports.md) 与 [DEC-002](../decisions/DEC-002-data-layer-boundary.md)。

## 本地规范性文档

- [产品与边界](../decisions/DEC-006-no-price-levels.md)
- [httpx 冲突处理](../decisions/DEC-007-httpx-conflict.md)
- [B 组范围裁定](../decisions/DEC-010-scope-ruling-a-retain-b-implement.md)
- [需求追踪矩阵](requirements-traceability.md)
- [CLI 手册](cli-reference.md)
