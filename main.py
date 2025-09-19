from fastapi import FastAPI
from pydantic import BaseModel
import ccxt

app = FastAPI()

# Example model for API requests
class TickerRequest(BaseModel):
    exchange: str
    symbol: str

@app.get("/")
def root():
    return {"message": "FastAPI + CCXT backend is running!"}

@app.post("/ticker")
def get_ticker(req: TickerRequest):
    try:
        exchange_class = getattr(ccxt, req.exchange)
        exchange = exchange_class()
        ticker = exchange.fetch_ticker(req.symbol)
        return {"symbol": req.symbol, "ticker": ticker}
    except Exception as e:
        return {"error": str(e)}
