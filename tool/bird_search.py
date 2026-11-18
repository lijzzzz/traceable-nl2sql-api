import os
from glob import glob
from typing import List, Union

from loguru import logger

from tool.openai_api import get_embedding_batch
from tool.spider_search import GraphSearcher
from tool.utils import init_chroma_client, is_dict
from utils import read_json, timeout

# 使用项目根目录的绝对路径，避免工作目录不一致导致文件找不到
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COL_VEC_PKL_FILE = os.path.join(
    ROOT_DIR, "database", "cache_vector_db", "bird_cols_tb_name_coldesc_3_large.pkl"
)


def load_bird_dbs():
    paths = glob(os.path.join(ROOT_DIR, "database", "cols_info", "bird", "*.json"))
    paths.sort()

    dbs = [os.path.basename(p).replace(".json", "") for p in paths]
    dbs.sort()
    return dbs


def get_sqlite_file(db):
    p1 = os.path.join(ROOT_DIR, "dataset", "bird", "train_databases", db, f"{db}.sqlite")
    p2 = os.path.join(ROOT_DIR, "dataset", "bird", "dev_databases", db, f"{db}.sqlite")
    if os.path.exists(p1):
        return p1
    elif os.path.exists(p2):
        return p2
    else:
        raise FileNotFoundError(f"Database {db} not found in train or dev databases.")


# -------------------------- search column -------------------------- #


def create_column_searcher(db):
    """
    column_searcher = create_column_searcher("your_db_name", "path_to_col_vec_pkl_file")
    results = column_searcher("your_query", topk=5)
    print(results)
    """

    dbs = load_bird_dbs()
    assert db in dbs, f"db `{db}` not found in {dbs}"

    chroma_client = init_chroma_client("bird")
    index_name = os.path.basename(COL_VEC_PKL_FILE).replace(".pkl", "")
    collection = chroma_client.get_collection(index_name)

    @timeout(60)
    def search_column(query: Union[str, List[str]], topk=15, str_mode=True):
        """
        str_mode: real use. each item one line.
        query: [q1, q2, ...]
        Return: {q1: [x1,x2,...], q2: [x1,x2,...], ...}
        """
        query = query if isinstance(query, list) else [query]
        results = {}
        for q in query:
            result = []
            q = q.strip().lower()

            # make desc of column
            desc = f"a column about {q}."

            # get embedding
            vec = get_embedding_batch([desc], model="text-embedding-3-large")
            result = collection.query(
                query_embeddings=vec[0], n_results=max(10, topk), where={"db": db}
            )

            # add distance to metadata
            format_results = []
            for metadata, distance in zip(
                result["metadatas"][0], result["distances"][0]
            ):
                new_metadata = {
                    "column": metadata["name"],
                    "format": metadata["data_format"],
                    "table": metadata["table"],
                }

                if "value_description" in metadata and metadata["value_description"]:
                    new_metadata["value_description"] = metadata["value_description"]

                if "statistics" in metadata:
                    _vd = metadata["statistics"]
                    if is_dict(_vd):
                        _vd = list(eval(_vd).keys())
                        _vd = f"categorical field. {_vd}"
                        if len(_vd) > 400:
                            _vd = _vd[:400] + f"...(Omit {len(_vd) - 400} chars)"
                    elif len(_vd) > 200:
                        _vd = _vd[:200] + f"...(Omit {len(_vd) - 200} chars)"
                    new_metadata["statistics"] = _vd

                # fix weird bug
                if q == metadata["name"].lower():
                    distance = 0.0
                new_metadata["distance"] = distance
                format_results.append(new_metadata)

            format_results.sort(key=lambda x: x["distance"])

            _topk = max(topk, len([i for i in format_results if i["distance"] == 0.0]))
            if _topk != topk:
                logger.warning(
                    f"DB: `{db}`, Query: `{q}`, Topk: {topk}, Num(distance==0): {_topk}"
                )
                topk = _topk

            if str_mode:
                for r in format_results[:topk]:
                    r.pop("distance")
            results[q] = format_results[:topk]

        if len(results) == 1:
            results = results[query[0].lower()]
        if str_mode:
            results = str(results)
        return results

    return search_column


# -------------------------- search path -------------------------- #


class GraphSearcherBIRD(GraphSearcher):
    def __init__(self) -> None:
        self.dbs_name = load_bird_dbs()
        self._nx_cache_dir = os.path.join(ROOT_DIR, ".cache", "nx_cache", "bird")
        self._nx_tmp_dir = os.path.join(ROOT_DIR, ".tmp", "nx", "bird")
        os.makedirs(self._nx_cache_dir, exist_ok=True)
        os.makedirs(self._nx_tmp_dir, exist_ok=True)
        self._node_pair_to_edge = {}
        self._G = {}

    def _get_sqlite_file(self, db: str):
        return get_sqlite_file(db)


g_searcher = GraphSearcherBIRD()


def create_path_finder(db: str):
    global g_searcher
    g_searcher.find_shortest_path(db=db, start="!@#$%^", end="!@#$%^")

    @timeout(60)
    def _find_shortest_path(
        start: Union[str, List[str]], end: Union[str, List[str]], debug=False
    ):
        return g_searcher.find_shortest_path(db=db, start=start, end=end, debug=debug)

    return _find_shortest_path


# -------------------------- search value -------------------------- #


def create_value_searcher(db):
    """
    db_has_text_filed_list: Output by preprocess/bird_db_indexing_es.py
    """
    db_has_text_filed_list = read_json(
        os.path.join(ROOT_DIR, "preprocess", "bird_db_has_text_filed_list.json")
    )
    if db in db_has_text_filed_list:
        from tool.client_es import ESClient

        es_client = ESClient()
        FLAG_GO = True
    else:
        FLAG_GO = False

    @timeout(60)
    def _search(
        query: Union[str, List[str]],
        table: Union[str, List[str]] = [],
        column: Union[str, List[str]] = [],
        topk=5,
        str_mode=True,
    ):
        """
        query: [q1, q2, ...]
        Return: {q1: [x1,x2,...], q2: [x1,x2,...], ...}
        """
        if not FLAG_GO:
            return f"Error. Tables in db `{db}` do not have text field."

        # 强制转换为字符串列表（兼容 LLM 传入 int/float 等类型）
        if not isinstance(query, list):
            query = [str(query)]
        else:
            query = [str(q) for q in query]

        if not isinstance(table, list):
            table = [str(table)] if table is not None else [None]
        else:
            table = [str(t) if t is not None else None for t in table]

        if not isinstance(column, list):
            column = [str(column)] if column is not None else [None]
        else:
            column = [str(c) if c is not None else None for c in column]

        if table == []:
            table = [None] * len(query)
        if column == []:
            column = [None] * len(query)

        if not len(query) == len(table) == len(column):
            return "Error. Length of query, table, column should be the same."

        results = {}

        queries = [{"match": {"contents": q}} for q in query]
        responses = es_client.msearch(
            index=f"bird-{db}".lower(), queries=queries, size=50
        )

        # ===== 防御：处理 msearch 可能的封装格式 =====
        if isinstance(responses, dict):
            if "responses" in responses:
                responses = responses["responses"]
            elif "error" in responses:
                logger.error(f"ES msearch global error: {responses['error']}")
                return f"Error: ES msearch failed - {responses['error']}"
        # =============================================

        for q, res, t, c in zip(query, responses, table, column):
            # ===== 防御：检查每个子查询的返回 =====
            if not isinstance(res, dict):
                logger.error(
                    f"ES response for query '{q}' is not dict, got {type(res)}: {str(res)[:200]}"
                )
                results[q] = [{"error": f"Unexpected response type: {type(res)}"}]
                continue

            if "error" in res:
                logger.error(f"ES query error for '{q}': {res['error']}")
                results[q] = [{"error": str(res['error'])}]
                continue

            if "hits" not in res:
                logger.error(
                    f"ES response missing 'hits' for query '{q}': {str(res)[:300]}"
                )
                results[q] = [{"error": "Missing hits in ES response"}]
                continue
            # =====================================

            hits = res["hits"]["hits"]
            result = []
            for hit in hits:
                doc = hit["_source"]
                if t and doc.get("table", None) != t:
                    continue
                if c and doc.get("column", None) != c:
                    continue
                doc["score"] = hit["_score"]
                doc.pop("id", None)
                result.append(doc)
            result = result[:topk]
            if str_mode:
                for r in result:
                    r.pop("score", None)
                    r.pop("db", None)
            results[q] = result

        if len(results) == 1:
            results = results[query[0]]

        return str(results) if str_mode else results

    return _search


if __name__ == "__main__":
    # python tool/bird_search.py

    # ---------- SearchColumn ----------
    # SearchColumn = create_column_searcher("card_games")
    # r = SearchColumn("language", topk=5)
    # print(r)

    # ---------- SearchPath ----------
    # path_finder = create_path_finder("card_games")
    # res = path_finder(start="sets.name", end=["set_translations.language"], debug=True)
    # print(res)

    # ---------- SearchValue ----------
    SearchValue = create_value_searcher("codebase_community")
    res = SearchValue(query="2010")
    print(res)