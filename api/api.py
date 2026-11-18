import sqlite3
import json
from datetime import datetime
import sys
import os

# 启动时自动加载项目根目录的 .env（若存在），便于本地配置密钥
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

# 获取项目根目录（api文件夹的上一级）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
import uuid
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
import ast
import re

# 解决 Windows 下 ES 配置找不到 HOME 的问题
if os.name == 'nt' and 'HOME' not in os.environ:
    os.environ['HOME'] = os.environ.get('USERPROFILE', '')

# 导入项目核心函数
from tool.action_execution import chat_with_LLM
from interactive_text_to_sql import load_schema_and_examples_dialog, load_tooldesc

# 创建 FastAPI 应用
app = FastAPI(
    title="可追溯 NL2SQL API",
    description="将自然语言问题转换为 SQL 并返回结构化结果，支持过程追溯",
    version="1.0.0"
)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_dataset_type(db_id: str) -> str:
    spider_db_path = os.path.join(ROOT_DIR, "dataset", "spider", "database", db_id, f"{db_id}.sqlite")
    if os.path.exists(spider_db_path):
        return "spider"
    bird_db_path = os.path.join(ROOT_DIR, "dataset", "bird", "dev_databases", db_id, f"{db_id}.sqlite")
    if os.path.exists(bird_db_path):
        return "bird"
    return "spider"

# ---------- 预加载全局资源 ----------
TOOLDESC = load_tooldesc("spider", add_evidence=False)
ALL_EXAMPLARS = load_schema_and_examples_dialog("spider", add_evidence=False)

DB_EXAMPLAR_MAP: Dict[str, str] = {}
for item in ALL_EXAMPLARS:
    lines = item.split('\n')
    if lines and lines[0].startswith("Schema of database "):
        raw_name = lines[0].replace("Schema of database ", "").replace(":", "").strip()
        if '\\' in raw_name:
            db_name = raw_name.split('\\')[-1]
        elif '/' in raw_name:
            db_name = raw_name.split('/')[-1]
        else:
            db_name = raw_name
        DB_EXAMPLAR_MAP[db_name] = item

DEFAULT_DB = "activity_1"

# ---------- 数据库初始化 ----------
def init_db():
    conn = sqlite3.connect("traces.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            question TEXT,
            db_id TEXT,
            sql_statement TEXT,
            execution_result TEXT,
            data_structure TEXT,
            natural_description TEXT,
            timestamp TEXT,
            dialog_path TEXT,
            response_json TEXT
        )
    ''')
    conn.commit()
    conn.close()

@app.on_event("startup")
async def startup_event():
    init_db()

# ---------- 数据模型 ----------
class QueryRequest(BaseModel):
    question: str = Field(..., description="自然语言问题")
    db_id: str = Field(DEFAULT_DB, description="数据库标识")
    model_name: str = Field("gpt-4o-mini", description="LLM 模型名称")
    add_evidence: bool = Field(False, description="是否使用证据（BIRD 数据集专用）")
    disabled_tools: Optional[List[str]] = Field(None, description="禁用的工具列表，如 ['SearchColumn', 'SearchValue']")

class QueryResponse(BaseModel):
    trace_id: str
    sql_statement: str
    execution_result: List[Dict]
    data_structure: Dict[str, str]
    natural_description: str

class TraceResponse(BaseModel):
    trace_id: str
    question: str
    db_id: str
    final_sql: str
    intermediate_steps: List[Dict]

# ---------- 辅助函数 ----------
def extract_final_sql_and_result(save_dir: str):
    import re
    json_files = [f for f in os.listdir(save_dir) if f.endswith('.json')]
    if not json_files:
        return "", [], "No result file."

    with open(os.path.join(save_dir, json_files[0]), 'r', encoding='utf-8') as f:
        data = json.load(f)

    dialog = data.get("dialog", [])
    final_sql = ""
    final_result = []
    description = ""

    original_question = ""
    for msg in dialog:
        if msg["role"] == "user" and "Q: " in msg["content"]:
            q_part = msg["content"].split("Q: ")[-1].strip()
            original_question = q_part
            break

    last_sql = None
    last_obs = None
    for i in range(len(dialog) - 1, -1, -1):
        msg = dialog[i]
        if msg["role"] == "assistant" and "ExecuteSQL(" in msg["content"]:
            match = re.search(r'ExecuteSQL\("(.*?)"\)', msg["content"], re.DOTALL)
            if match:
                last_sql = match.group(1).strip()
                for j in range(i + 1, len(dialog)):
                    if dialog[j]["role"] == "user" and dialog[j]["content"].startswith("Observation:"):
                        last_obs = dialog[j]["content"].replace("Observation:", "").strip()
                        break
                break

    if last_sql:
        final_sql = last_sql
        if last_obs:
            if "(Hint:" in last_obs:
                last_obs = last_obs.split("(Hint:")[0].strip()
            try:
                result_list = ast.literal_eval(last_obs)
                if result_list and isinstance(result_list[0], tuple):
                    final_result = [dict(zip(range(len(row)), row)) for row in result_list]
                else:
                    final_result = result_list
            except:
                final_result = []

    if final_result:
        row_count = len(final_result)
        if original_question:
            q_lower = original_question.lower()
            if q_lower.startswith("what are "):
                core = original_question[9:].strip()
            elif q_lower.startswith("what is "):
                core = original_question[8:].strip()
            else:
                core = original_question
            core = core.rstrip('?')
            description = f"查询{core}，共返回 {row_count} 条记录。"
        else:
            description = f"查询成功，共返回 {row_count} 条记录。"

        if row_count > 0 and row_count <= 5:
            examples = []
            for row in final_result:
                values = list(row.values())
                if len(values) == 1:
                    examples.append(str(values[0]))
                else:
                    examples.append(" - ".join(str(v) for v in values))
            description += " 结果：" + "；".join(examples)
        elif row_count > 5:
            examples = []
            for row in final_result[:2]:
                values = list(row.values())
                if len(values) == 1:
                    examples.append(str(values[0]))
                else:
                    examples.append(" - ".join(str(v) for v in values))
            description += f" 例如：{examples[0]}；{examples[1]} 等。"
    else:
        description = "未查询到相关数据。"

    return final_sql, final_result, description

# ---------- API 端点 ----------
@app.post("/query", response_model=QueryResponse, summary="自然语言转SQL")
async def query(request: QueryRequest):
    trace_id = str(uuid.uuid4())

    # 1. 获取该数据库对应的 schema+examples
    db_examplar = DB_EXAMPLAR_MAP.get(request.db_id)
    if not db_examplar:
        db_examplar = f"Schema of database {request.db_id}:\n(no detailed schema available)"
        logger.warning(f"Database {request.db_id} not in preloaded schemas, using empty schema.")

    # 2. 根据 disabled_tools 过滤工具描述
    if request.disabled_tools:
        tool_blocks = TOOLDESC.split('\n\n')
        filtered_blocks = []
        for block in tool_blocks:
            block_stripped = block.strip()
            disabled = False
            for dt in request.disabled_tools:
                # 匹配工具名后紧跟 '(' 或 换行
                if block_stripped.startswith(f"{dt}(") or block_stripped.startswith(f"{dt}\n"):
                    disabled = True
                    break
            if not disabled:
                filtered_blocks.append(block)
        filtered_TOOLDESC = '\n\n'.join(filtered_blocks)
    else:
        filtered_TOOLDESC = TOOLDESC

    tooldesc_demos = filtered_TOOLDESC + "\n\n" + db_examplar

    # 3. 准备临时保存目录
    save_dir = f"./api_traces/{trace_id}"
    os.makedirs(save_dir, exist_ok=True)

    # 4. 构造 item 字典
    item = {
        "id": trace_id,
        "question": request.question,
        "db_id": request.db_id,
        "evidence": "",
        "SQL": ""
    }

    try:
        dataset_type = get_dataset_type(request.db_id)
        model_name_to_use = os.environ.get("MODEL_NAME", request.model_name)
        result_path = chat_with_LLM(
            d=item,
            model_name=model_name_to_use,
            save_dir=save_dir,
            tooldesc_demos=tooldesc_demos,
            max_round_num=12,
            dataset=dataset_type,
            add_evidence=request.add_evidence,
            disabled_tools=request.disabled_tools,
        )
        if result_path is None:
            # 超时或内部错误导致无结果
            logger.error(f"Trace {trace_id} failed: chat_with_LLM returned None (likely timeout)")
            raise HTTPException(status_code=504, detail="LLM inference timeout after 5 minutes")
        logger.info(f"Trace {trace_id} saved to {result_path}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"LLM inference failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM inference error: {str(e)}")

    # 5. 提取最终结果
    final_sql, exec_result, description = extract_final_sql_and_result(save_dir)

    # 6. 构造 data_structure
    data_structure = {}
    if exec_result and len(exec_result) > 0:
        if isinstance(exec_result[0], dict):
            for k, v in exec_result[0].items():
                data_structure[str(k)] = type(v).__name__
        elif isinstance(exec_result[0], (list, tuple)):
            for i, val in enumerate(exec_result[0]):
                data_structure[str(i)] = type(val).__name__
        else:
            data_structure = {"message": "No data"}
    else:
        data_structure = {"message": "No data"}

    # 7. 保存到数据库
    response_obj = QueryResponse(
        trace_id=trace_id,
        sql_statement=final_sql,
        execution_result=exec_result,
        data_structure=data_structure,
        natural_description=description
    )
    response_dict = response_obj.model_dump()
    response_json_str = json.dumps(response_dict, ensure_ascii=False)

    try:
        conn = sqlite3.connect("traces.db")
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO traces 
            (trace_id, question, db_id, sql_statement, execution_result, data_structure, natural_description, timestamp, dialog_path, response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trace_id,
            request.question,
            request.db_id,
            final_sql,
            json.dumps(exec_result, ensure_ascii=False),
            json.dumps(data_structure, ensure_ascii=False),
            description,
            datetime.now().isoformat(),
            save_dir,
            response_json_str
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save trace to database: {e}")

    return response_obj

@app.get("/trace/{trace_id}", response_model=TraceResponse, summary="获取查询追溯详情")
async def get_trace(trace_id: str):
    conn = sqlite3.connect("traces.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT question, db_id, sql_statement, execution_result, natural_description, dialog_path FROM traces WHERE trace_id = ?",
        (trace_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Trace not found")

    question, db_id, sql_statement, exec_result_json, description, dialog_path = row
    exec_result = json.loads(exec_result_json) if exec_result_json else []

    # 从 dialog_path 读取中间步骤（可选）
    intermediate_steps = []
    if dialog_path and os.path.exists(dialog_path):
        json_files = [f for f in os.listdir(dialog_path) if f.endswith('.json')]
        if json_files:
            with open(os.path.join(dialog_path, json_files[0]), 'r', encoding='utf-8') as f:
                data = json.load(f)
                for msg in data.get("dialog", []):
                    if msg["role"] == "assistant":
                        content = msg.get("content", "")
                        thought = ""
                        action = ""
                        if "Thought:" in content:
                            parts = content.split("Action:")
                            thought = parts[0].replace("Thought:", "").strip()
                            if len(parts) > 1:
                                action = parts[1].strip()
                        intermediate_steps.append({"type": "assistant", "thought": thought, "action": action})
                    elif msg["role"] == "user" and msg["content"].startswith("Observation:"):
                        intermediate_steps.append(
                            {"type": "observation", "content": msg["content"].replace("Observation:", "").strip()})

    return TraceResponse(
        trace_id=trace_id,
        question=question,
        db_id=db_id,
        final_sql=sql_statement,
        intermediate_steps=intermediate_steps
    )


@app.get("/trace/{trace_id}/flowchart")
async def get_trace_flowchart(trace_id: str):
    """
    生成 Mermaid 流程图，展示推理步骤的因果链
    """
    import os
    import json
    import re
    import ast

    trace_path = os.path.join("api_traces", trace_id, f"{trace_id}.json")
    if not os.path.exists(trace_path):
        raise HTTPException(status_code=404, detail="Trace not found")

    with open(trace_path, "r", encoding="utf-8") as f:
        trace = json.load(f)

    dialog = trace.get("dialog", [])
    if not dialog:
        return {"flowchart": "graph TD\n    Start[No steps recorded]"}

    # 智能摘要函数：根据观察内容生成简短描述
    def summarize_observation(content: str) -> str:
        # 去掉 "Observation:" 前缀
        if content.startswith("Observation:"):
            content = content[12:].strip()
        # 尝试解析为 Python 列表（如 SearchColumn 返回的列信息）
        try:
            data = ast.literal_eval(content)
            if isinstance(data, list) and len(data) > 0:
                # 判断是否为列信息（包含 'column', 'table' 等键）
                if all(isinstance(item, dict) and 'column' in item for item in data):
                    columns = [item['column'] for item in data[:5]]
                    col_str = ", ".join(columns)
                    if len(data) > 5:
                        col_str += f" 等{len(data)}列"
                    return f"找到列：{col_str}"
                # 判断是否为元组列表（SQL 执行结果）
                elif isinstance(data[0], tuple):
                    return f"返回 {len(data)} 行数据"
                else:
                    return f"返回 {len(data)} 条记录"
        except:
            pass
        # 如果内容过长，截取前60字符
        if len(content) > 60:
            return content[:57] + "..."
        return content

    # 清洗标签：只保留安全字符
    def clean_label(text: str, max_len=40) -> str:
        # 移除所有非字母、数字、中文、空格、简单标点（.?!:;,）
        cleaned = re.sub(r'[^\w\u4e00-\u9fff\s\.\?!:;,()]', ' ', text)
        # 将多个空格合并为一个
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len-3] + "..."
        # 转义双引号
        cleaned = cleaned.replace('"', '\\"')
        return cleaned

    mermaid_lines = ["graph TD"]
    step_index = 0
    previous_node_id = None

    for msg in dialog:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant":
            # 解析 Thought 和 Action
            thought = ""
            action = ""
            if "Thought:" in content:
                parts = content.split("Action:")
                thought = parts[0].replace("Thought:", "").strip()
                if len(parts) > 1:
                    action = parts[1].strip()
            else:
                # 如果没有 Thought 格式，可能是最终 Done
                if "Done" in content:
                    action = "Done"
                else:
                    thought = content[:60]

            # 添加 Thought 节点（矩形）
            if thought:
                thought_label = clean_label(f"💭 {thought}")
                thought_node = f"n{step_index}"
                mermaid_lines.append(f'    {thought_node}["{thought_label}"]')
                if previous_node_id:
                    mermaid_lines.append(f"    {previous_node_id} --> {thought_node}")
                previous_node_id = thought_node
                step_index += 1

            # 添加 Action 节点（圆角矩形）
            if action:
                action_label = clean_label(f"🔧 {action}")
                action_node = f"n{step_index}"
                mermaid_lines.append(f'    {action_node}("{action_label}")')
                if previous_node_id:
                    mermaid_lines.append(f"    {previous_node_id} --> {action_node}")
                previous_node_id = action_node
                step_index += 1

        elif role == "user" and "Observation:" in content:
            # 观察结果：生成摘要并作为菱形节点
            obs_summary = summarize_observation(content)
            obs_label = clean_label(f"👁️ {obs_summary}")
            obs_node = f"n{step_index}"
            # 菱形节点语法：id{{label}}
            mermaid_lines.append(f'    {obs_node}{{{{{obs_label}}}}}')
            if previous_node_id:
                mermaid_lines.append(f"    {previous_node_id} --> {obs_node}")
            previous_node_id = obs_node
            step_index += 1
            # 如果包含错误，高亮
            if "error" in content.lower() or "fail" in content.lower():
                mermaid_lines.append(f"    style {obs_node} fill:#ffcccc,stroke:#f00,stroke-width:2px")

    # 如果没有生成任何节点，添加默认开始节点
    if len(mermaid_lines) == 1:
        mermaid_lines.append('    Start["无有效步骤"]')

    flowchart = "\n".join(mermaid_lines)
    return {"flowchart": flowchart}

@app.get("/traces", summary="获取所有历史查询记录")
async def get_traces(limit: int = 20):
    conn = sqlite3.connect("traces.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT trace_id, question, timestamp FROM traces ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"trace_id": row[0], "question": row[1], "timestamp": row[2]} for row in rows]

@app.get("/query_result/{trace_id}", summary="获取完整查询结果")
async def get_query_result(trace_id: str):
    conn = sqlite3.connect("traces.db")
    cursor = conn.cursor()
    cursor.execute("SELECT response_json FROM traces WHERE trace_id = ?", (trace_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Result not found")
    return json.loads(row[0])

@app.get("/health", summary="健康检查")
async def health():
    return {"status": "ok"}


# ---------- 启动说明 ----------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)