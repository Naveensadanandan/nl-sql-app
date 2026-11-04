from fastapi import FastAPI

from nlsql.api.routes import ask

app = FastAPI(title="NL-SQL")
app.include_router(ask.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
