# ultra_bot.py
"""
Ultra Smart Crypto Signal Bot
- Binance WebSocket for multi-symbol trade/agg data
- Indicators: SMA, EMA, MACD, RSI, Bollinger Bands, Volume Momentum, simple ADX-like
- Signal aggregator: needs N confirmations (configurable)
- Telegram subscription management & affiliate link injection
- Persist last signals and subscribers to JSON (replace with DB for production)
"""

import os
import asyncio
import json
import time
import math
from typing import Dict, List, Optional

import aiohttp
import websockets
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set in env")

COINS = [c.strip().upper() for c in os.getenv("COINS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",") if c.strip()]
INTERVAL = os.getenv("INTERVAL", "1m")  # we will map to binance stream (e.g., kline_1m)
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "10"))  # fallback loop delay if needed

# Indicator params
SHORT_WINDOW = int(os.getenv("SHORT_WINDOW", "7"))
LONG_WINDOW = int(os.getenv("LONG_WINDOW", "25"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STDDEV = float(os.getenv("BB_STDDEV", "2.0"))

# Aggregation rule: how many indicators must agree to emit a signal
CONFIRMATIONS_NEEDED = int(os.getenv("CONFIRMATIONS_NEEDED", "4"))

SUBSCRIBERS_FILE = os.getenv("SUBSCRIBERS_FILE", "ultra_subs.json")
LAST_SIGNALS_FILE = os.getenv("ULTRA_LAST_SIGNALS", "ultra_last_signals.json")
AFFILIATE_LINK = os.getenv("AFFILIATE_LINK", "")

BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream?streams="

# helper IO
def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)

def load_subscribers() -> List[int]:
    return load_json(SUBSCRIBERS_FILE, [])

def save_subscribers(subs: List[int]):
    save_json(SUBSCRIBERS_FILE, subs)

def load_last_signals() -> Dict[str, Dict]:
    return load_json(LAST_SIGNALS_FILE, {})

def save_last_signals(d: Dict):
    save_json(LAST_SIGNALS_FILE, d)

# --- Indicators ---
def sma(prices: List[float], window: int) -> float:
    if not prices:
        return 0.0
    if len(prices) < window:
        return sum(prices) / len(prices)
    return sum(prices[-window:]) / window

def ema_series(prices: List[float], window: int) -> List[float]:
    if not prices:
        return []
    k = 2 / (window + 1)
    emas = [prices[0]]
    for p in prices[1:]:
        emas.append(p * k + emas[-1] * (1 - k))
    return emas

def macd(prices: List[float], fast=12, slow=26, signal=9):
    if len(prices) < slow:
        return 0.0, 0.0, 0.0
    fast_ema = ema_series(prices, fast)
    slow_ema = ema_series(prices, slow)
    # align lengths
    if len(fast_ema) > len(slow_ema):
        fast_ema = fast_ema[-len(slow_ema):]
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema_series(macd_line, signal)[-1] if len(macd_line) >= signal else sum(macd_line)/len(macd_line)
    macd_val = macd_line[-1]
    hist = macd_val - signal_line
    return macd_val, signal_line, hist

def rsi(prices: List[float], period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def bollinger(prices: List[float], period=20, dev=2.0):
    if len(prices) < period:
        mean = sum(prices)/len(prices) if prices else 0.0
        std = (sum((p-mean)**2 for p in prices)/len(prices))**0.5 if prices else 0.0
        return mean, mean+dev*std, mean-dev*std
    slicep = prices[-period:]
    mean = sum(slicep)/period
    std = (sum((p-mean)**2 for p in slicep)/period)**0.5
    upper = mean + dev*std
    lower = mean - dev*std
    return mean, upper, lower

def volume_momentum(vols: List[float], window=10):
    if len(vols) < window+1:
        return 1.0
    recent = vols[-window:]
    prev = vols[-(window*2):-window] if len(vols) >= window*2 else vols[:len(recent)]
    avg_recent = sum(recent)/len(recent)
    avg_prev = sum(prev)/len(prev) if prev else avg_recent
    return avg_recent / (avg_prev if avg_prev>0 else 1.0)

# simple ADX-like (very rough approximation using directional movement on closes)
def simple_trend_strength(prices: List[float], period=14):
    if len(prices) < period+1:
        return 0.0
    diffs = [abs(prices[i]-prices[i-1]) for i in range(1, len(prices))]
    return sum(diffs[-period:]) / period

# --- Binance WebSocket handling (kline streams) ---
# We'll subscribe to each coin's kline stream for the chosen INTERVAL (e.g., 1m)
def build_ws_url(coins: List[str], interval: str):
    streams = []
    # binance expects lowercase symbol@kline_interval
    for c in coins:
        streams.append(f"{c.lower()}@kline_{interval}")
    return BINANCE_WS_BASE + "/".join(streams)

# parse kline payload -> return close price and volume
def parse_kline(event):
    k = event.get("k", {})
    is_final = k.get("x", False)  # whether candle is closed
    close = float(k.get("c", 0.0))
    volume = float(k.get("v", 0.0))
    return is_final, close, volume

# --- Signal logic aggregator ---
def evaluate_indicators(prices: List[float], vols: List[float]):
    """
    Returns a dict with indicator boolean signals: buy_sma, buy_macd, buy_rsi, buy_bb, buy_vol
    """
    indicators = {}
    short = sma(prices, SHORT_WINDOW)
    longv = sma(prices, LONG_WINDOW)
    indicators["sma_trend"] = short > longv  # True => bullish
    macd_val, signal_line, hist = macd(prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    # bullish when hist > 0 and rising
    indicators["macd_bull"] = hist > 0
    curr_rsi = rsi(prices, RSI_PERIOD)
    indicators["rsi_ok_buy"] = curr_rsi < 70  # not overbought
    mean_bb, upper_bb, lower_bb = bollinger(prices, BB_PERIOD, BB_STDDEV)
    indicators["bb_touch_low"] = prices[-1] <= lower_bb  # touching lower band
    vol_m = volume_momentum(vols)
    indicators["vol_increasing"] = vol_m > 1.05
    indicators["strength"] = simple_trend_strength(prices)
    indicators["rsi_value"] = curr_rsi
    indicators["macd_hist"] = hist
    indicators["sma_short"] = short
    indicators["sma_long"] = longv
    indicators["bb_mean"] = mean_bb
    return indicators

# --- Messaging helpers ---
async def broadcast_signal(bot: Bot, subscribers: List[int], text: str):
    for cid in subscribers:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception as e:
            print(f"Failed send to {cid}: {e}")

# --- Main orchestrator ---
async def ultra_loop(app):
    bot: Bot = app.bot
    subscribers = load_subscribers()
    last_signals = load_last_signals()
    # per-symbol price history (in-memory). For production use persistent DB or cache.
    hist_prices: Dict[str, List[float]] = {s: [] for s in COINS}
    hist_vols: Dict[str, List[float]] = {s: [] for s in COINS}
    # build ws url
    ws_url = build_ws_url(COINS, INTERVAL)
    print("Connecting to:", ws_url)
    # We'll also have a REST fallback to bootstrap history
    async with aiohttp.ClientSession() as session:
        # bootstrap initial candles via REST
        for sym in COINS:
            try:
                params = {"symbol": sym, "interval": INTERVAL, "limit": 500}
                async with session.get("https://api.binance.com/api/v3/klines", params=params, timeout=10) as resp:
                    data = await resp.json()
                    closes = [float(k[4]) for k in data]
                    vols = [float(k[5]) for k in data]
                    hist_prices[sym] = closes[-500:]
                    hist_vols[sym] = vols[-500:]
            except Exception as e:
                print(f"Bootstrap REST for {sym} failed: {e}")
                hist_prices[sym] = hist_prices.get(sym, [])
                hist_vols[sym] = hist_vols.get(sym, [])

    # Connect websocket and process kline updates
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                print("WS connected")
                async for msg in ws:
                    try:
                        payload = json.loads(msg)
                    except Exception:
                        continue
                    data = payload.get("data") or {}
                    stream = payload.get("stream", "")
                    # parse symbol from stream (like btcusdt@kline_1m)
                    sym = stream.split("@")[0].upper()
                    is_final, close, vol = parse_kline(data)
                    # append last price/volume (even if not final keep updating for indicators)
                    if sym in hist_prices:
                        hist_prices[sym].append(close)
                        hist_vols[sym].append(vol)
                        # cap history to reasonable size
                        if len(hist_prices[sym]) > 1000:
                            hist_prices[sym] = hist_prices[sym][-1000:]
                        if len(hist_vols[sym]) > 1000:
                            hist_vols[sym] = hist_vols[sym][-1000:]
                    # Decide signals only when candle closed to avoid noise
                    if not is_final:
                        continue

                    indicators = evaluate_indicators(hist_prices[sym], hist_vols[sym])
                    # Count bullish / bearish votes
                    bull_votes = 0
                    bear_votes = 0
                    # rules (tunable):
                    if indicators["sma_short"] > indicators["sma_long"]:
                        bull_votes += 1
                    else:
                        bear_votes += 1
                    if indicators["macd_hist"] > 0:
                        bull_votes += 1
                    else:
                        bear_votes += 1
                    if indicators["rsi_value"] < 30:
                        bull_votes += 1  # oversold -> potential buy
                    if indicators["rsi_value"] > 70:
                        bear_votes += 1
                    if indicators["bb_touch_low"]:
                        bull_votes += 1
                    # volume momentum as confirmation
                    if indicators["vol_increasing"]:
                        bull_votes += 1
                    # strength threshold
                    if indicators["strength"] > 0:
                        bull_votes += 0  # not decisive

                    signal = None
                    if bull_votes >= CONFIRMATIONS_NEEDED:
                        signal = "BUY"
                    elif bear_votes >= CONFIRMATIONS_NEEDED:
                        signal = "SELL"

                    # dedupe & persist last signal
                    last = last_signals.get(sym, {}).get("signal")
                    if signal and last == signal:
                        # skip duplicate
                        continue

                    if signal:
                        price = hist_prices[sym][-1]
                        text = (f"🔔 <b>{signal} SIGNAL</b>\n"
                                f"Symbol: {sym}\nPrice: {price:.6f}\n"
                                f"SMA{SHORT_WINDOW}/{LONG_WINDOW}: {indicators['sma_short']:.6f}/{indicators['sma_long']:.6f}\n"
                                f"MACD hist: {indicators['macd_hist']:.6f}\n"
                                f"RSI: {indicators['rsi_value']:.2f}\n"
                                f"Vol momentum: {volume_momentum(hist_vols[sym]):.2f}\n"
                                f"Confirmations: {bull_votes if signal=='BUY' else bear_votes}/{CONFIRMATIONS_NEEDED}\n")
                        if AFFILIATE_LINK:
                            text += f"\n▶ Trade via referral: {AFFILIATE_LINK}"
                        # send to subscribers
                        subs = load_subscribers()
                        asyncio.create_task(broadcast_signal(bot, subs, text))
                        # update last signals
                        last_signals[sym] = {"signal": signal, "price": price, "time": int(time.time())}
                        save_last_signals(last_signals)
        except Exception as e:
            print("WS error, reconnecting:", e)
            await asyncio.sleep(3)

# --- Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("Բարև! Ես Ultra Smart Signal Bot եմ (Binance). "
           "Use /subscribe to get signals. Use /coins to see tracked coins. "
           "Premium signals via subscription will be available soon.")
    await update.message.reply_text(txt)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = load_subscribers()
    cid = update.effective_chat.id
    if cid in subs:
        await update.message.reply_text("Դուք արդեն բաժանորդագրված եք։")
        return
    subs.append(cid)
    save_subscribers(subs)
    await update.message.reply_text("Բաժանորդագրված եք։ Դուք կստանաք ծրագրի սիգնալները։")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = load_subscribers()
    cid = update.effective_chat.id
    if cid not in subs:
        await update.message.reply_text("Դուք չէք բաժանորդագրված։")
        return
    subs.remove(cid)
    save_subscribers(subs)
    await update.message.reply_text("Դու հեռացվեցիր բաժանորդներից։")

async def coins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tracked coins:\n" + ", ".join(COINS))

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price SYMBOL (e.g., /price BTCUSDT)")
        return
    sym = context.args[0].upper()
    async with aiohttp.ClientSession() as session:
        try:
            params = {"symbol": sym, "interval": INTERVAL, "limit": 100}
            async with session.get("https://api.binance.com/api/v3/klines", params=params, timeout=10) as resp:
                data = await resp.json()
                closes = [float(k[4]) for k in data]
                vols = [float(k[5]) for k in data]
                short = sma(closes, SHORT_WINDOW)
                longv = sma(closes, LONG_WINDOW)
                macd_val, sig, hist = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                curr_rsi = rsi(closes, RSI_PERIOD)
                mean_bb, up_bb, low_bb = bollinger(closes, BB_PERIOD, BB_STDDEV)
                text = (f"{sym} price: {closes[-1]:.6f}\nSMA{SHORT_WINDOW}/{LONG_WINDOW}: {short:.6f}/{longv:.6f}\nMACD hist: {hist:.6f}\nRSI: {curr_rsi:.2f}\nBB: {low_bb:.6f} - {up_bb:.6f}")
                if AFFILIATE_LINK:
                    text += f"\n\nTrade via: {AFFILIATE_LINK}"
                await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Failed to fetch {sym}: {e}")

ADMIN_ID = os.getenv("ADMIN_ID")
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text("You are not admin.")
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    subs = load_subscribers()
    for cid in subs:
        try:
            await context.bot.send_message(chat_id=cid, text=f"📢 ADMIN:\n\n{msg}")
        except Exception:
            pass
    await update.message.reply_text("Broadcast sent.")

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("coins", coins_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    return app

# Entrypoint
if __name__ == "__main__":
    app = build_app()
    async def main():
        async with app:
            # start websocket loop in background
            asyncio.create_task(ultra_loop(app))
            await app.start()
            print("Ultra Smart Bot started.")
            await app.updater.start_polling(poll_interval=1.0)
            await asyncio.Event().wait()
    asyncio.run(main())
