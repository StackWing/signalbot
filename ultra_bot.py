# signal_bot.py
import os
import asyncio
import requests
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise RuntimeError("TELEGRAM_TOKEN and CHAT_ID must be set in env")

bot = Bot(token=TOKEN)

# 10 coins
COINS = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "DASHUSDT", "SHIBUSDT",
    "SOLUSDT", "BNBUSDT", "TRXUSDT", "DOGEUSDT", "ADAUSDT"
]

# % change to trigger signal
SIGNAL_THRESHOLD = float(os.getenv("SIGNAL_THRESHOLD", "1.5"))

# keep recent signals to detect trend (last 3)
SIGNAL_HISTORY = {coin: [] for coin in COINS}
# keep price when we last sent a signal for each coin
LAST_SIGNAL_PRICE = {coin: None for coin in COINS}

async def get_price(symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

def fmt_price_diff(curr, prev):
    """Return tuple (diff$, diff%) with nice formatting"""
    diff = curr - prev
    try:
        pct = (diff / prev) * 100
    except Exception:
        pct = 0.0
    return diff, pct

async def send_signal(coin, direction, price, change):
    """Send BUY/SELL signal including since-last-signal delta if exists"""
    emoji = "🟢" if direction == "BUY" else "🔴"
    header = f"{emoji} *{direction} SIGNAL* — {coin}\n💰 Price: `${price:.6f}`\n📊 Change: {change:+.2f}%\n"
    extra = ""
    last_price = LAST_SIGNAL_PRICE.get(coin)
    if last_price is not None:
        diff, pct = fmt_price_diff(price, last_price)
        sign = "+" if diff >= 0 else ""
        extra = f"📈 Since last signal: {sign}{diff:.6f}$ ({pct:+.2f}%)\n"
    # message assembly
    msg = header + extra
    # send
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    # update last signal price for this coin
    LAST_SIGNAL_PRICE[coin] = price

async def send_trend(coin, trend):
    trend_emoji = "🚀" if trend == "BUY" else "⚠️"
    msg = f"{trend_emoji} *Market Trend Alert — {coin}*\nCurrent Trend: *Strong {trend}*"
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

async def main():
    last_prices = {}

    while True:
        for coin in COINS:
            price = await get_price(coin)
            if price is None:
                continue

            if coin in last_prices:
                old_price = last_prices[coin]
                change = ((price - old_price) / old_price) * 100

                if abs(change) >= SIGNAL_THRESHOLD:
                    direction = "BUY" if change > 0 else "SELL"
                    # send the main signal (includes since-last-signal diff if present)
                    await send_signal(coin, direction, price, change)

                    # update history for trend detection
                    SIGNAL_HISTORY[coin].append(direction)
                    if len(SIGNAL_HISTORY[coin]) > 3:
                        SIGNAL_HISTORY[coin].pop(0)

                    # if last 3 signals are same -> send trend alert and clear history
                    if len(SIGNAL_HISTORY[coin]) == 3 and len(set(SIGNAL_HISTORY[coin])) == 1:
                        await send_trend(coin, direction)
                        SIGNAL_HISTORY[coin].clear()

                    print(f"Signal sent: {coin} -> {direction} ({change:.2f}%)")

            # store last polled price
            last_prices[coin] = price

        await asyncio.sleep(30)  # poll interval (seconds)

if __name__ == "__main__":
    asyncio.run(main())

