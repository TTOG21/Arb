from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any

app = FastAPI(title="Arbitrage Backend")

# ---------------------------
# 🔹 Enable CORS (frontend calls won't fail)
# ---------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to ["http://localhost:3000"] in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# 🔹 Health check
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# ---------------------------
# 🔹 Screener endpoints
# ---------------------------
@app.post("/screener/start")
def screener_start():
    # TODO: start arbitrage logic here
    return {"message": "Screener started"}

@app.get("/screener/status")
def screener_status():
    # TODO: return actual screener status
    return {"status": "running", "active_pairs": 0}

# ---------------------------
# 🔹 Trade log
# ---------------------------
@app.get("/get_trade_log")
def get_trade_log(limit: int = 10):
    # TODO: fetch from DB or memory
    logs: List[Dict[str, Any]] = []
    return {"limit": limit, "logs": logs}

# ---------------------------
# 🔹 Triangular arbitrage finder
# ---------------------------
@app.get("/find_triangular_arbitrage")
def find_triangular_arbitrage():
    # TODO: add CCXT logic to scan exchanges
    return {"opportunity": None}
