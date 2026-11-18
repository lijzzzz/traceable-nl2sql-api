import json
import os
import sqlite3
import threading
from typing import List

from tenacity import retry, stop_after_attempt, wait_fixed, wait_random_exponential
from openai import OpenAI, Timeout

# ========== 统一的客户端（同时用于 Chat 和 Embedding） ==========
# 密钥从环境变量读取，切勿将真实 key 写进代码或提交到仓库。
# 复制 .env.example 为 .env 并填入自己的 OPENAI_API_KEY 即可运行。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError(
        "未检测到 OPENAI_API_KEY 环境变量。请在项目根目录创建 .env 文件并填入密钥，"
        "或通过环境变量提供；参考 .env.example。"
    )

# 如需使用兼容 OpenAI 接口的第三方服务，可通过 OPENAI_BASE_URL 覆盖；
# 留空则默认使用 OpenAI 官方地址。
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL") or None

openai_client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0),
    base_url=OPENAI_BASE_URL,
)

# 可用的 Embedding 模型列表（保持不变）
OPENAI_EMBEDDING_MODELS = [
    "text-embedding-ada-002",
    "text-embedding-3-small",
    "text-embedding-3-large",
]


@retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(3))
def chatgpt(
    prompt="Hello!",
    system_content="You are an AI assistant.",
    messages=None,
    model="gpt-4o-mini",              # ← 默认使用 gpt-4o-mini（便宜）
    temperature=0,
    top_p=0.95,
    n=1,
    stop=None,
    max_tokens=512,                   # 适当增大，避免截断
    presence_penalty=0,
    frequency_penalty=0,
    logit_bias={},
    **kwargs,
):
    assert model is not None, "model name is None"
    messages = messages if messages else [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt},
    ]
    # 使用统一的 openai_client（中转）
    response = openai_client.chat.completions.create(
        model=model, messages=messages, temperature=temperature,
        top_p=top_p, n=n, stop=stop, max_tokens=max_tokens,
        presence_penalty=presence_penalty, frequency_penalty=frequency_penalty,
        logit_bias=logit_bias,
    )
    return json.loads(response.model_dump_json())


# ========== SQLite 向量缓存（不变） ==========
thread_local = threading.local()

def get_sqlite_client():
    if not hasattr(thread_local, "cache_sql_client"):
        os.makedirs("database/cache_vector_query", exist_ok=True)
        cache_db_path = "database/cache_vector_query/local_cache.db"
        thread_local.cache_sql_client = sqlite3.connect(cache_db_path)
        cursor = thread_local.cache_sql_client.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS vec_cache (
            name TEXT PRIMARY KEY, vec TEXT NOT NULL);""")
        thread_local.cache_sql_client.commit()
    return thread_local.cache_sql_client

def get_vec_cache(name):
    cursor = get_sqlite_client().cursor()
    cursor.execute("SELECT vec FROM vec_cache WHERE name=?;", (name,))
    res = cursor.fetchone()
    return json.loads(res[0]) if res else None

def insert_vec_cache(name, vec):
    if get_vec_cache(name):
        return
    vec = json.dumps(vec) if not isinstance(vec, str) else vec
    cursor = get_sqlite_client().cursor()
    cursor.execute("INSERT INTO vec_cache (name, vec) VALUES (?, ?);", (name, vec))
    get_sqlite_client().commit()


# ========== Embedding API（同样走中转） ==========
@retry(wait=wait_fixed(5), stop=stop_after_attempt(2))
def get_embedding(text: str, model="text-embedding-3-large") -> list[float]:
    """默认使用 text-embedding-3-large（与论文一致）"""
    assert model in OPENAI_EMBEDDING_MODELS
    text_unikey = text + model
    res = get_vec_cache(text_unikey)
    if res:
        return res
    res = openai_client.embeddings.create(input=[text], model=model).data[0].embedding
    insert_vec_cache(text_unikey, res)
    return res

@retry(wait=wait_fixed(5), stop=stop_after_attempt(2))
def get_embedding_batch(texts: List[str], model="text-embedding-3-large") -> list[float]:
    assert model in OPENAI_EMBEDDING_MODELS
    unseen = [t for t in texts if get_vec_cache(t + model) is None]
    if unseen:
        req = openai_client.embeddings.create(input=unseen, model=model, timeout=120.0)
        for t, emb in zip(unseen, [i.embedding for i in req.data]):
            insert_vec_cache(t + model, emb)
    return [get_vec_cache(t + model) for t in texts]


# ========== Tokenizer（不变） ==========
chatgpt_tok = None
def chatgpt_tokenize(text):
    global chatgpt_tok
    if chatgpt_tok is None:
        import tiktoken
        chatgpt_tok = tiktoken.encoding_for_model("gpt-4o-2024-05-13")
    return chatgpt_tok.encode(text)

embedding_tok = None
def embedding_tokenize(text):
    global embedding_tok
    if embedding_tok is None:
        import tiktoken
        embedding_tok = tiktoken.encoding_for_model("text-embedding-3-small")
    return embedding_tok.encode(text)