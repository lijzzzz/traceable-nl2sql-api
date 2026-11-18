import sqlite3

from tool.utils import (
    contain_in_clause,
    contain_multi_columns_in_select_clause,
    contain_op_in_select_clause,
    extract_table_alias,
    extract_where_clause,
)
from utils import timeout


def create_execute_sql(db, _hint=True):
    db_file = f"dataset/spider/test_database/{db}/{db}.sqlite"
    FLAG = True
    FLAG_table_alias = True

    def set_flag():
        nonlocal FLAG
        FLAG = False

    def get_flag():
        nonlocal FLAG
        return FLAG

    @timeout(100)
    def execute_sql(sql, str_mode=True):
        if isinstance(sql, str):
            sql = sql.strip()
            while (sql.startswith('"') and sql.endswith('"')) or (sql.startswith("'") and sql.endswith("'")):
                sql = sql[1:-1].strip()
        # ========== 统一清洗 SQL ==========
        print(f"[DEBUG] SQL received: {repr(sql)}")
        if isinstance(sql, str):
            sql = sql.strip()
            # 1. 去除 Markdown 代码块标记
            if sql.startswith("```sql"):
                sql = sql[6:]
            if sql.startswith("```"):
                sql = sql[3:]
            if sql.endswith("```"):
                sql = sql[:-3]
            # 2. 循环去掉开头和结尾的所有引号（包括多层）
            while sql and (sql[0] in ('"', "'")):
                sql = sql[1:]
            while sql and (sql[-1] in ('"', "'")):
                sql = sql[:-1]
            # 3. 只取第一条 SQL（分号前的内容），防止多语句执行错误
            if ';' in sql:
                sql = sql.split(';')[0].strip()
            # 4. 确保以 SELECT 开头（忽略前导字符）
            sql = sql.strip()
            upper_sql = sql.upper()
            select_idx = upper_sql.find("SELECT")
            if select_idx > 0:
                sql = sql[select_idx:]
        # ====================================
        # 接下去的原有逻辑保持不变
        _sql = sql.lower()
        if "(select" in sql:
            # 注意：这里可能有误，应该是 _sql 而不是 sql？原代码如此，保留
            _sql = sql[: sql.index("(select", 0)]

        # rule: left join is not allowed
        if _hint and str_mode and " left join " in _sql:
            return "Error. Do not support 'left join'."

        # rule: Do not support table alias
        nonlocal FLAG_table_alias
        if _hint and str_mode and FLAG_table_alias and extract_table_alias(_sql):
            FLAG_table_alias = False
            return f"Error. Do not use table alias, please write the full table name."

        # rule: Do not support 'in' syntax
        if _hint and str_mode and contain_in_clause(_sql):
            return "Error. Do not support 'where ... in (...)' syntax, please use 'OR' instead."

        # trick: Do not support '_id = ' in where clause
        where_clause = extract_where_clause(_sql)
        if _hint and str_mode and "_id = " in where_clause:
            return "Error. Do not use '_id = ' in where clause, please write the full condition."

        connection = sqlite3.connect(db_file)
        connection.text_factory = lambda b: b.decode(errors="ignore")
        cursor = connection.cursor()

        nonlocal FLAG
        try:
            cursor.execute(sql)
            result = cursor.fetchall()
            result = sorted(set(result))
            if str_mode:
                result = str(result)
                # 注释掉结果截断和提示（保持干净）
            cursor.close()
            return result
        except Exception as e:
            cursor.close()
            return str(e)

    execute_sql.set_flag = set_flag
    execute_sql.get_flag = get_flag

    return execute_sql


if __name__ == "__main__":
    ExecuteSQL = create_execute_sql(db="network_1")
    r = ExecuteSQL(
        "SELECT ID FROM Highschooler EXCEPT (SELECT student_id FROM Friend UNION SELECT friend_id FROM Friend)"
    )
    print(r)
