from fastapi import FastAPI

from app.routers import orders, users

app = FastAPI(title="Sample Shop")

app.include_router(users.router)
app.include_router(orders.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
