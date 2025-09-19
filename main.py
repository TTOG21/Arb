from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/screener/start")
def start_screener():
    return {"message": "Screener started"}

@app.get("/screener/status")
def screener_status():
    return {"status": "running"}

@app.get("/get_trade_log")
def get_trade_log(limit: int = 10):
    return {"logs": []}

@app.get("/find_triangular_arbitrage")
def find_triangular_arbitrage():
    return {"opportunity": None}
