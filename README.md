# 可追溯NL2SQL API服务与交互系统

> 将一个基于大语言模型的交互式Text-to-SQL方法，工程化为一套**可调用的RESTful API服务**与**可视化网页应用**，并为其补上**全流程可追溯**能力：每一次自然语言到SQL的转换，都能回放模型的多轮思考、工具调用与观察结果，并生成一张Mermaid推理流程图。

本项目的核心工作是**服务化与产品化**：在已有研究方法的基础上，设计并实现了HTTP接口层、持久化追溯存储、结构化结果解析、前端交互界面与一键式运行配置，把一个研究脚本变成可被第三方系统调用的完整服务。

---

## 目录

- [项目亮点](#项目亮点)
- [技术栈](#技术栈)
- [系统架构](#系统架构)
- [API接口](#api-接口)
- [可追溯机制](#可追溯机制)
- [网页应用](#网页应用)
- [快速开始](#快速开始)
- [数据与运行说明](#数据与运行说明)
- [项目结构](#项目结构)
- [致谢与来源](#致谢与来源)

---

## 项目亮点

- **服务化封装**：用FastAPI将Text-to-SQL推理封装为标准HTTP服务，提供查询、追溯、历史记录、健康检查等6个接口，自动生成Swagger交互式文档。
- **全流程可追溯**：为每个请求分配唯一 `trace_id`，把自然语言问题、最终SQL、执行结果、中间推理步骤一并持久化到SQLite，支持事后回放。
- **推理过程可视化**：把多轮Thought / Action / Observation链路自动转换成Mermaid流程图，前端一键渲染成因果图，让模型的每一步决策可解释、可审计。
- **结构化结果解析**：从模型的自由文本输出中稳健提取最终SQL与执行结果，推断字段类型并生成自然语言摘要，屏蔽底层解析细节。
- **可配置的工具与模型**：支持通过请求参数动态禁用检索工具，支持环境变量切换模型与接口地址，便于在不同环境部署。
- **配套的网页应用**：基于Tailwind CSS的单页应用，集成查询输入、结果表格、历史记录、文本追溯、流程图和JSON / CSV导出。

---

## 技术栈

| 层次 | 技术 |
| --- | --- |
| Web服务 | FastAPI、Uvicorn、Pydantic |
| 前端 | HTML、Tailwind CSS、JavaScript、Mermaid |
| 大模型与向量 | OpenAI API、Embedding、Chroma、Elasticsearch |
| 数据存储 | SQLite（追溯记录与向量缓存） |
| 配置管理 | python-dotenv、环境变量 |

---

## 系统架构

```
浏览器(web/demo.html)
        │  HTTP / JSON
        ▼
FastAPI 服务(api/api.py)
        │
        ├── /query ──────► 参数校验 ─► 工具裁剪 ─► Agent 推理(tool/)
        │                                            │
        │                                   多轮 思考/行动/观察
        │                                            ▼
        │                                  最终 SQL + 执行结果解析
        │                                            │
        │                                  持久化(SQLite traces.db)
        │                                            │
        ├── /trace/{id} ─────────────► 回放中间推理步骤
        ├── /trace/{id}/flowchart ──► 生成 Mermaid 流程图
        ├── /traces ────────────────► 历史查询列表
        ├── /query_result/{id} ─────► 完整结果回读
        └── /health ────────────────► 健康检查
```

底层Agent方法采用多轮交互范式：模型先根据数据库schema与工具描述进行思考，再调用检索工具（列检索、值检索、最短路径查找）逐步收集信息，最终调用 `ExecuteSQL` 生成并执行SQL。该原理图见 [`docs/upstream_overview.png`](docs/upstream_overview.png)（来自上游项目）。

---

## API接口

服务启动后访问 `http://localhost:8000/docs` 可查看自动生成的Swagger文档。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/query` | 自然语言转SQL并执行，返回SQL、结果、字段结构与自然语言摘要 |
| `GET` | `/trace/{trace_id}` | 获取某次查询的中间推理步骤（思考与观察） |
| `GET` | `/trace/{trace_id}/flowchart` | 生成该次查询的Mermaid流程图代码 |
| `GET` | `/traces` | 获取最近的查询历史记录 |
| `GET` | `/query_result/{trace_id}` | 回读某次查询的完整结构化结果 |
| `GET` | `/health` | 健康检查 |

`POST /query` 请求体示例：

```json
{
  "question": "What are the names of all students?",
  "db_id": "activity_1",
  "model_name": "gpt-4o-mini",
  "add_evidence": false,
  "disabled_tools": ["SearchValue"]
}
```

响应体示例：

```json
{
  "trace_id": "8b1f...c3d2",
  "sql_statement": "SELECT name FROM student",
  "execution_result": [{"0": "Alice"}, {"0": "Bob"}],
  "data_structure": {"0": "str"},
  "natural_description": "查询所有学生的姓名，共返回 2 条记录。 结果：Alice；Bob"
}
```

---

## 可追溯机制

每一次查询都会生成全局唯一的 `trace_id`，并串联起三类信息：

1. **结果追溯**：最终SQL、执行结果、字段类型与自然语言摘要写入 `traces` 表，可随时回读。
2. **过程追溯**：模型每一轮的Thought与Action、工具返回的Observation保存为对话轨迹，`/trace/{id}` 将其解析为结构化步骤。
3. **可视化追溯**：`/trace/{id}/flowchart` 把上述链路编译为Mermaid流程图，用不同形状区分思考节点、动作节点与观察节点，并对报错节点高亮。

这一机制让"模型为什么生成这条SQL"变得可查、可看、可解释，是本项目相对于原始方法的主要增强点。

---

## 网页应用

`web/demo.html` 是一个无需构建的单文件前端，直接用浏览器打开即可（需先启动后端服务）。功能包括：

- 自然语言问题输入框与数据库ID选择
- 查询结果展示：生成的SQL、自然语言描述、字段结构、结果表格
- 历史查询列表，点击即可回看任意一次结果
- "文本步骤"与"流程图"两种追溯视图切换
- 结果导出为JSON或CSV
- 180秒请求超时保护与友好的错误提示

> 提示：建议在此处补一张系统运行截图（查询输入 + 结果 + 流程图），比文字描述更直观。

---

## 快速开始

**1. 克隆仓库**

```bash
git clone <your-repo-url>.git
cd traceable-nl2sql-api
```

**2. 创建虚拟环境并安装依赖**

```bash
python -m venv .venv
source .venv/bin/activate      # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt
```

**3. 配置密钥**

```bash
cp .env.example .env
```

编辑 `.env`，填入你自己的 `OPENAI_API_KEY`。使用OpenAI官方接口时无需填写 `OPENAI_BASE_URL`。

**4. 启动服务**

```bash
python -m uvicorn api.api:app --reload --host 0.0.0.0 --port 8000
```

**5. 打开界面**

- Swagger文档：`http://localhost:8000/docs`
- 网页应用：直接用浏览器打开 `web/demo.html`
- 健康检查：`http://localhost:8000/health`

启动后即可用 `/health` 和 `/docs` 验证服务正常；实际执行 `/query` 需要先准备好下方的数据资源。

---

## 数据与运行说明

为控制仓库体积、避免版权与隐私问题，本仓库**不包含大规模数据集、向量库和第三方服务配置**。若需让 `/query` 端到端跑通，请自行准备：

| 资源 | 放置位置 | 用途 |
| --- | --- | --- |
| Spider数据集 | `dataset/spider/` | 提供SQLite数据库，供SQL执行与列/值检索 |
| Chroma向量库 | `database/db_chroma_spider/` | 列与表名的向量检索（SearchColumn） |
| Elasticsearch | 本机或远程服务 + `~/.es_config.json` | 文本字段的值检索（SearchValue） |

仓库已内置一个最小可运行示例所需的提示词模板（`prompt/spider/`）与 `activity_1` 数据库的schema描述（`database/dbs_info/spider/activity_1.md`），用于展示输入构造方式。若未配置向量库或ES，对应检索工具会降级返回提示，但不影响服务启动与接口演示。

> 数据集获取：Spider数据集可从其官方网站或公开仓库下载，按上表目录结构放置即可。

---

## 项目结构

```
traceable-nl2sql-api/
├── api/                      # FastAPI 服务层（个人实现）
│   └── api.py                # 接口定义、追溯存储、结果解析
├── web/                      # 前端应用层（个人实现）
│   └── demo.html             # 单页交互界面
├── tool/                     # Agent 推理与检索工具（基于上游方法改造）
│   ├── action_execution.py   # 多轮推理主循环
│   ├── spider_search.py      # 列/值检索、最短路径
│   ├── spider_execution.py   # SQL 执行
│   ├── openai_api.py         # 模型与 Embedding 调用（密钥读环境变量）
│   └── ...
├── prompt/                   # 提示词模板（工具描述 + 示例）
├── database/                 # schema 描述与向量库（向量库未提交）
├── preprocess/               # 预处理配置
├── docs/                     # 文档与原理图
├── requirements.txt
├── .env.example              # 配置模板（不含真实密钥）
└── .gitignore
```

---

## 致谢与来源

- 本项目底层的多轮交互式Text-to-SQL Agent方法，改造自开源项目 **Interactive-Text-to-SQL**。原始原理图见 [`docs/upstream_overview.png`](docs/upstream_overview.png)，版权归原作者所有。
- 评估脚本思路参考DAMO-ConvAI / BIRD与test-suite-sql-eval（本仓库未包含评估代码）。
- 本人在其中的工作集中在：RESTful API服务设计与实现、可追溯机制（持久化存储 + 过程回放 + Mermaid可视化）、结构化结果解析、前端交互应用，以及工程化配置与文档。

---

*Author: Anonymous*
