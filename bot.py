import asyncio
import aiohttp
import time
import sys
import os
from collections import defaultdict, deque
from datetime import datetime

# ================= НАСТРОЙКИ =================

# Лучше хранить токен в Railway -> Variables: BOT_TOKEN
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8419391176:AAH7wjseRf5x0Pw0Op53X3GzyHqgJY_9-PM")
CHANNEL = os.environ.get("CHANNEL", "@bybitoialert")

POLL_SECONDS = 3

# Пороги (%)
TICK_THRESHOLD = 4.0     # между двумя опросами (~3с)
W5_THRESHOLD = 4.0       # за 5 сек
W10_THRESHOLD = 4.0      # за 10 сек

W5_SEC = 5
W10_SEC = 10

COOLDOWN_SEC = 180
ONLY_USDT = True

KEEP_SEC = 40            # хранить историю (должно быть > 10)

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
TG_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Заголовки (часто помогают против 403/WAF)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OIBot/1.0; +https://railway.app)",
    "Accept": "application/json",
}

# ================= СОСТОЯНИЕ =================

prev_oi = {}                             # symbol -> last oi
oi_hist = defaultdict(lambda: deque())   # symbol -> deque[(ts, oi)]
last_alert = {}                          # symbol -> ts

# ================= TELEGRAM =================

async def tg_send(session: aiohttp.ClientSession, text: str):
    url = TG_SEND_URL.format(token=BOT_TOKEN)
    payload = {"chat_id": CHANNEL, "text": text, "disable_web_page_preview": True}
    async with session.post(url, json=payload, timeout=20) as r:
        body = await r.text()
        if r.status != 200:
            print(f"❌ TG send error HTTP {r.status}: {body[:300]}")
            sys.stdout.flush()

# ================= BYBIT =================

async def fetch_tickers(session: aiohttp.ClientSession):
    async with session.get(BYBIT_TICKERS_URL, headers=HEADERS, timeout=20) as r:
        if r.status != 200:
            txt = await r.text()
            # важно: на 403 часто приходит HTML (<TITLE>ERROR)
            raise RuntimeError(f"Bybit HTTP {r.status}: {txt[:200]}")
        data = await r.json()

    if str(data.get("retCode", 0)) != "0":
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

    return data["result"]["list"]

def nearest_value(hist: deque, target_ts: float):
    """Вернуть значение oi, ближайшее по времени к target_ts."""
    if not hist:
        return None
    best_val = None
    best_dt = float("inf")
    for ts, v in hist:
        dt = abs(ts - target_ts)
        if dt < best_dt:
            best_dt = dt
            best_val = v
    return best_val

def fmt_pct(x):
    return "n/a" if x is None else f"{x:+.2f}%"

# ================= MAIN =================

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_TOKEN_HERE":
        print("❌ BOT_TOKEN не задан. Добавь BOT_TOKEN в Railway -> Variables.")
        return

    print(
        f"✅ BOT STARTED {datetime.now().strftime('%H:%M:%S')} | poll={POLL_SECONDS}s | "
        f"thr: tick={TICK_THRESHOLD}% 5s={W5_THRESHOLD}% 10s={W10_THRESHOLD}%"
    )
    sys.stdout.flush()

    last_alive = 0
    backoff = 1

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tickers = await fetch_tickers(session)
                now = time.time()

                for t in tickers:
                    symbol = t.get("symbol")
                    if not symbol:
                        continue
                    if ONLY_USDT and not symbol.endswith("USDT"):
                        continue

                    try:
                        oi = float(t.get("openInterest") or 0)
                    except ValueError:
                        continue
                    if oi <= 0:
                        continue

                    # история
                    oi_hist[symbol].append((now, oi))
                    while oi_hist[symbol] and now - oi_hist[symbol][0][0] > KEEP_SEC:
                        oi_hist[symbol].popleft()

                    # 1) tick (% между двумя опросами)
                    old_tick = prev_oi.get(symbol)
                    prev_oi[symbol] = oi
                    if old_tick is None or old_tick == 0:
                        continue

                    oi_tick_pct = (oi - old_tick) / old_tick * 100.0

                    # 2) 5s / 10s
                    old_5s = nearest_value(oi_hist[symbol], now - W5_SEC)
                    old_10s = nearest_value(oi_hist[symbol], now - W10_SEC)

                    oi_5s_pct = None
                    oi_10s_pct = None
                    if old_5s and old_5s != 0:
                        oi_5s_pct = (oi - old_5s) / old_5s * 100.0
                    if old_10s and old_10s != 0:
                        oi_10s_pct = (oi - old_10s) / old_10s * 100.0

                    # триггеры (рост и падение)
                    trig_tick = abs(oi_tick_pct) >= TICK_THRESHOLD
                    trig_5s = (oi_5s_pct is not None) and (abs(oi_5s_pct) >= W5_THRESHOLD)
                    trig_10s = (oi_10s_pct is not None) and (abs(oi_10s_pct) >= W10_THRESHOLD)

                    if not (trig_tick or trig_5s or trig_10s):
                        continue

                    # антиспам
                    last = last_alert.get(symbol, 0)
                    if COOLDOWN_SEC and now - last < COOLDOWN_SEC:
                        continue

                    # направление (по самому сильному изменению)
                    candidates = [oi_tick_pct]
                    if oi_5s_pct is not None:
                        candidates.append(oi_5s_pct)
                    if oi_10s_pct is not None:
                        candidates.append(oi_10s_pct)
                    main_delta = max(candidates, key=lambda x: abs(x))
                    direction = "📈" if main_delta > 0 else "📉"

                    # какие окна сработали — эмодзи
                    flags = []
                    if trig_tick:
                        flags.append("⚡")   # tick (~3s)
                    if trig_5s:
                        flags.append("⏱")   # 5s
                    if trig_10s:
                        flags.append("🕙")  # 10s
                    flags_str = " ".join(flags)

                    msg = (
                        f"{direction} {symbol}\n"
                        f"{flags_str}\n\n"
                        f"OI:\n"
                        f"tick(~{POLL_SECONDS}s): {oi_tick_pct:+.2f}%\n"
                        f"5s:  {fmt_pct(oi_5s_pct)}\n"
                        f"10s: {fmt_pct(oi_10s_pct)}"
                    )

                    await tg_send(session, msg)
                    last_alert[symbol] = now

                # alive
                if now - last_alive >= 30:
                    print(f"🟦 alive {datetime.now().strftime('%H:%M:%S')}")
                    sys.stdout.flush()
                    last_alive = now

                backoff = 1
                await asyncio.sleep(POLL_SECONDS)

            except asyncio.CancelledError:
                print("🛑 BOT STOPPED (cancelled)")
                return
            except Exception as e:
                print(f"⚠️ loop error: {e}")
                sys.stdout.flush()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 STOPPED BY USER (Ctrl+C)")
