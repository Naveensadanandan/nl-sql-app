from sqlalchemy import create_engine, inspect
from nlsql.core.config import get_settings
from nlsql.agent.state import AgentState

def get_table_schema(table: str, schema: str = 'public') -> 'dict':
    engine = create_engine(get_settings().database_url)
    insp = inspect(engine)
    return {
        "table": table,
        "columns": [
            {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
            for c in insp.get_columns(table, schema=schema)
        ],
        "primary_key": insp.get_pk_constraint(table, schema=schema)["constrained_columns"],
        "foreign_keys": insp.get_foreign_keys(table, schema=schema),
    }

def fetch_schema(state: AgentState) -> dict:
    table_name = "retail_sales"
    return {"schema": get_table_schema(table = table_name)}

