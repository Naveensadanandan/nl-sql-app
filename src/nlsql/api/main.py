from fastapi import FastAPI

from nlsql.api.routes import ambiguity, ask

app = FastAPI(title="NL-SQL")
app.include_router(ask.router)
app.include_router(ambiguity.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
