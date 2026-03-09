import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import matplotlib.pyplot as plt

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"

BASE = "https://xtracker.polymarket.com/api"
DEFAULT_USER = "elonmusk"
DEFAULT_PLATFORM = "X"
DEFAULT_TZ = "America/New_York"

STATE_FILE = Path("bot_state.json")
OUT_DIR = Path("tg_reports")
OUT_DIR.mkdir(exist_ok=True)

DEFAULT_MODE = "pm"   # pm | all | original
WATCH_INTERVAL = 60


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "mode": DEFAULT_MODE,
            "watch_enabled": False,
            "last_seen_post_id": None,
        }
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "mode": DEFAULT_MODE,
            "watch_enabled": False,
            "last_seen_post_id": None,
        }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


STATE = load_state()


def parse_dt(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"Bad datetime: {s}")


def daterange(d0, d1):
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)


def iso_z(dt_utc: datetime) -> str:
    return dt_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False

    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) elon_tweet_bot/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Connection": "close",
        }
    )
    return s


def get_user(handle: str, platform: str) -> dict:
    session = make_session()
    r = session.get(f"{BASE}/users/{handle}", params={"platform": platform}, timeout=60)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(j)
    return j["data"]


def get_posts(handle: str, start_utc: datetime, end_utc: datetime, platform: str) -> list[dict]:
    session = make_session()
    params = {
        "platform": platform,
        "startDate": iso_z(start_utc),
        "endDate": iso_z(end_utc),
    }

    last_err = None
    for attempt in range(1, 6):
        try:
            r = session.get(f"{BASE}/users/{handle}/posts", params=params, timeout=60)
            r.raise_for_status()
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(j)
            return j["data"]
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise last_err


def pick_ts(post: dict) -> datetime | None:
    for key in ("createdAt", "created_at", "created_at_utc", "timestamp", "created"):
        v = post.get(key)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def classify(post: dict) -> str:
    text = (post.get("content") or post.get("text") or "").strip()
    lower = text.lower()

    # 1. Явный тип из API, если есть
    for key in ("type", "postType", "kind", "__typename"):
        v = post.get(key)
        if isinstance(v, str) and v.strip():
            x = v.strip().lower()
            if x in {"reply", "repost", "quote", "original"}:
                return x
            if x in {"retweet", "retweeted"}:
                return "repost"
            if x in {"tweet", "post"}:
                return "original"

    # 2. Reply
    if text.startswith("@"):
        return "reply"

    if any(post.get(k) for k in [
        "inReplyToTweetId",
        "in_reply_to_status_id",
        "inReplyToId",
        "replyToId",
        "inReplyTo",
        "isReply",
    ]):
        return "reply"

    # 3. Repost
    if any(post.get(k) for k in [
        "retweetedTweet",
        "retweetedTweetId",
        "repostedTweet",
        "repostedTweetId",
        "retweetId",
        "repostId",
        "isRetweet",
        "isRepost",
    ]):
        return "repost"

    if text.startswith("RT @"):
        # спорные RT можно исключать
        if "rt @elonmusk" in lower:
            return "ignore"
        return "repost"

    # 4. Quote
    if any(post.get(k) for k in [
        "quotedTweet",
        "quotedTweetId",
        "quotedStatus",
        "quoteOf",
        "quoteId",
        "isQuote",
    ]):
        return "quote"

    if ("x.com/" in lower or "twitter.com/" in lower) and "/status/" in lower:
        return "quote"

    # 5. Original
    return "original"


def mode_to_include(mode: str) -> set[str]:
    mode = mode.lower()
    if mode == "pm":
        return {"original", "quote", "repost"}
    if mode == "all":
        return {"original", "quote", "repost", "reply"}
    if mode == "original":
        return {"original"}
    raise ValueError("Bad mode")


def mode_label(mode: str) -> str:
    if mode == "pm":
        return "pm (original+quote+repost, no reply)"
    if mode == "all":
        return "all"
    if mode == "original":
        return "original only"
    return mode


def build_report(start_local: datetime, end_local: datetime, mode: str):
    tz = ZoneInfo(DEFAULT_TZ)
    include_set = mode_to_include(mode)

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    handle = DEFAULT_USER.lstrip("@")
    _ = get_user(handle, DEFAULT_PLATFORM)
    posts = get_posts(handle, start_utc, end_utc, DEFAULT_PLATFORM)

    filtered = []
    day_buckets = {}
    hour_buckets = {h: {"original": 0, "reply": 0, "repost": 0, "quote": 0, "all": 0} for h in range(24)}

    start_day = start_local.date()
    end_day_inclusive = (end_local - timedelta(seconds=1)).date()

    for d in daterange(start_day, end_day_inclusive):
        day_buckets[d] = {"original": 0, "reply": 0, "repost": 0, "quote": 0, "all": 0}

    for p in posts:
        dt_utc = pick_ts(p)
        if not dt_utc:
            continue

        local_dt = dt_utc.astimezone(tz)
        if not (start_local <= local_dt < end_local):
            continue

        kind = classify(p)
        if kind == "ignore":
            continue

        day = local_dt.date()
        hour = local_dt.hour

        if day not in day_buckets:
            day_buckets[day] = {"original": 0, "reply": 0, "repost": 0, "quote": 0, "all": 0}

        day_buckets[day][kind] += 1
        day_buckets[day]["all"] += 1

        hour_buckets[hour][kind] += 1
        hour_buckets[hour]["all"] += 1

        filtered.append(
            {
                "id": p.get("id"),
                "url": p.get("url") or p.get("link"),
                "createdAt_utc": dt_utc.isoformat(),
                "createdAt_local": local_dt.isoformat(),
                "kind": kind,
                "content": (p.get("content") or p.get("text") or "").strip(),
                "raw_keys": sorted(list(p.keys())),
                "raw": p,
            }
        )

    daily_rows = []
    total_pm = 0
    type_totals = {"original": 0, "reply": 0, "repost": 0, "quote": 0}

    for d in sorted(day_buckets.keys()):
        b = day_buckets[d]
        pm_total = sum(b[k] for k in include_set)
        total_pm += pm_total

        for k in type_totals:
            type_totals[k] += b[k]

        daily_rows.append(
            {
                "date_et": d.isoformat(),
                "original": b["original"],
                "reply": b["reply"],
                "repost": b["repost"],
                "quote": b["quote"],
                "all": b["all"],
                "pm_total": pm_total,
            }
        )

    hourly_rows = []
    for h in range(24):
        b = hour_buckets[h]
        pm_total = sum(b[k] for k in include_set)
        hourly_rows.append(
            {
                "hour_et": f"{h:02d}",
                "original": b["original"],
                "reply": b["reply"],
                "repost": b["repost"],
                "quote": b["quote"],
                "all": b["all"],
                "pm_total": pm_total,
            }
        )

    daily_df = pd.DataFrame(daily_rows)
    hourly_df = pd.DataFrame(hourly_rows)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"report_{timestamp}.csv"
    png_path = OUT_DIR / f"heatmap_{timestamp}.png"
    dump_path = OUT_DIR / f"dump_{timestamp}.json"

    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("section,date_or_hour,original,reply,repost,quote,all,pm_total\n")
        for r in daily_rows:
            f.write(f"daily,{r['date_et']},{r['original']},{r['reply']},{r['repost']},{r['quote']},{r['all']},{r['pm_total']}\n")
        for r in hourly_rows:
            f.write(f"hourly,{r['hour_et']},{r['original']},{r['reply']},{r['repost']},{r['quote']},{r['all']},{r['pm_total']}\n")

    with open(dump_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)

    if filtered:
        heat_raw = pd.DataFrame(
            [
                {
                    "date_et": item["createdAt_local"][:10],
                    "hour_et": datetime.fromisoformat(item["createdAt_local"]).hour,
                    "count": 1,
                }
                for item in filtered
                if item["kind"] in include_set
            ]
        )
        if not heat_raw.empty:
            heat = (
                heat_raw.groupby(["date_et", "hour_et"])["count"]
                .sum()
                .unstack(fill_value=0)
                .reindex(columns=list(range(24)), fill_value=0)
            )
        else:
            heat = pd.DataFrame(0, index=[x["date_et"] for x in daily_rows], columns=list(range(24)))
    else:
        heat = pd.DataFrame(0, index=[x["date_et"] for x in daily_rows], columns=list(range(24)))

    plt.figure(figsize=(16, max(4, len(heat.index) * 0.45)))
    plt.imshow(heat.values, aspect="auto")
    plt.colorbar(label="Posts per hour")

    plt.xticks(range(24), [f"{h:02d}" for h in range(24)])
    plt.yticks(range(len(heat.index)), heat.index)
    plt.xlabel("Hour ET")
    plt.ylabel("Date ET")
    plt.title(f"@{DEFAULT_USER} hourly heatmap | mode={mode_label(mode)}")

    for y in range(heat.shape[0]):
        for x in range(heat.shape[1]):
            val = int(heat.iat[y, x])
            if val > 0:
                plt.text(x, y, str(val), ha="center", va="center", fontsize=8)

    plt.tight_layout()
    plt.savefig(png_path, dpi=180, bbox_inches="tight")
    plt.close()

    return {
        "start_local": start_local,
        "end_local": end_local,
        "mode": mode,
        "include_set": include_set,
        "total_pm": total_pm,
        "type_totals": type_totals,
        "daily_df": daily_df,
        "hourly_df": hourly_df,
        "csv_path": csv_path,
        "png_path": png_path,
        "dump_path": dump_path,
        "filtered": filtered,
    }


def format_df_text(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df.empty:
        return "No data"
    text = df.head(max_rows).to_string(index=False)
    if len(df) > max_rows:
        text += f"\n... ({len(df) - max_rows} more rows)"
    return text


def format_summary(report: dict) -> str:
    tl = report["type_totals"]
    return (
        f"Elon Musk report\n\n"
        f"Window ET:\n"
        f"{report['start_local'].strftime('%Y-%m-%d %H:%M')} -> {report['end_local'].strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Mode: {mode_label(report['mode'])}\n"
        f"pm_total: {report['total_pm']}\n\n"
        f"original: {tl['original']}\n"
        f"quote: {tl['quote']}\n"
        f"repost: {tl['repost']}\n"
        f"reply: {tl['reply']}"
    )


HELP_TEXT = """
Commands:

/help
/status

/range 2026-02-23 2026-02-25
/range 2026-02-23 12:00 2026-02-25 12:00

/today

/debugrange 2026-02-23 2026-02-25

/setmode pm
/setmode all
/setmode original

/watch_on
/watch_off
""".strip()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"User: {DEFAULT_USER}\n"
        f"Mode: {mode_label(STATE['mode'])}\n"
        f"Timezone: {DEFAULT_TZ}\n"
        f"Watch: {'ON' if STATE['watch_enabled'] else 'OFF'}\n"
        f"Last seen post id: {STATE.get('last_seen_post_id')}"
    )
    await update.message.reply_text(text)


def parse_range_args(args: list[str]):
    if len(args) == 2:
        start_str = args[0]
        end_str = args[1]
    elif len(args) == 4:
        start_str = f"{args[0]} {args[1]}"
        end_str = f"{args[2]} {args[3]}"
    else:
        raise ValueError("Use /range YYYY-MM-DD YYYY-MM-DD or /range YYYY-MM-DD HH:MM YYYY-MM-DD HH:MM")

    tz = ZoneInfo(DEFAULT_TZ)
    start_local = parse_dt(start_str).replace(tzinfo=tz)

    # ВАЖНО: если дата без времени, считаем end включительно по день
    if len(end_str.strip()) == 10:
        end_date = parse_dt(end_str).date()
        end_local = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz) + timedelta(seconds=1)
    else:
        end_local = parse_dt(end_str).replace(tzinfo=tz)

    return start_local, end_local


async def send_report(update: Update, report: dict):
    summary = format_summary(report)
    await update.message.reply_text(summary)

    daily_text = "Daily breakdown:\n\n" + format_df_text(report["daily_df"])
    await update.message.reply_text(f"<pre>{daily_text}</pre>", parse_mode="HTML")

    hourly_text = "Hourly breakdown:\n\n" + format_df_text(report["hourly_df"])
    await update.message.reply_text(f"<pre>{hourly_text}</pre>", parse_mode="HTML")

    with open(report["csv_path"], "rb") as f:
        await update.message.reply_document(InputFile(f, filename=report["csv_path"].name))

    with open(report["png_path"], "rb") as f:
        await update.message.reply_photo(InputFile(f, filename=report["png_path"].name))


async def cmd_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        start_local, end_local = parse_range_args(context.args)
        report = build_report(start_local, end_local, STATE["mode"])
        await send_report(update, report)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tz = ZoneInfo(DEFAULT_TZ)
        now_local = datetime.now(tz)
        start_local = datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=tz)
        end_local = start_local + timedelta(days=1)

        report = build_report(start_local, end_local, STATE["mode"])
        await send_report(update, report)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_debugrange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        start_local, end_local = parse_range_args(context.args)
        report = build_report(start_local, end_local, STATE["mode"])

        lines = [
            "Debug range",
            f"Window ET: {start_local.strftime('%Y-%m-%d %H:%M')} -> {end_local.strftime('%Y-%m-%d %H:%M')}",
            f"Mode: {mode_label(report['mode'])}",
            f"Counted posts: {len(report['filtered'])}",
            "",
            "Last classified posts:",
        ]

        sample = report["filtered"][-20:]
        for item in sample:
            lines.append(
                f"{item['createdAt_local']} | {item['kind']} | {(item['content'] or '')[:90]}"
            )

        text = "\n".join(lines)
        await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

        with open(report["dump_path"], "rb") as f:
            await update.message.reply_document(InputFile(f, filename=report["dump_path"].name))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_setmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) != 1:
            raise ValueError("Use /setmode pm | all | original")
        mode = context.args[0].lower().strip()
        if mode not in {"pm", "all", "original"}:
            raise ValueError("Use /setmode pm | all | original")
        STATE["mode"] = mode
        save_state(STATE)
        await update.message.reply_text(f"Mode set to: {mode_label(mode)}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_watch_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE["watch_enabled"] = True
    save_state(STATE)
    await update.message.reply_text("Watch enabled.")


async def cmd_watch_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE["watch_enabled"] = False
    save_state(STATE)
    await update.message.reply_text("Watch disabled.")


async def watcher(context: ContextTypes.DEFAULT_TYPE):
    if not STATE.get("watch_enabled"):
        return

    try:
        tz = ZoneInfo(DEFAULT_TZ)
        now_local = datetime.now(tz)
        start_local = now_local - timedelta(hours=24)
        end_local = now_local

        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        posts = get_posts(DEFAULT_USER, start_utc, end_utc, DEFAULT_PLATFORM)

        visible = []
        for p in posts:
            ts = pick_ts(p)
            if not ts:
                continue
            local_dt = ts.astimezone(tz)
            if not (start_local <= local_dt < end_local):
                continue
            kind = classify(p)
            if kind == "ignore":
                continue

            visible.append(
                {
                    "id": p.get("id"),
                    "url": p.get("url") or p.get("link"),
                    "kind": kind,
                    "content": (p.get("content") or p.get("text") or "").strip(),
                    "local_dt": local_dt,
                }
            )

        visible.sort(key=lambda x: x["local_dt"])

        if not visible:
            return

        newest = visible[-1]
        newest_id = newest["id"]

        if newest_id and newest_id != STATE.get("last_seen_post_id"):
            STATE["last_seen_post_id"] = newest_id
            save_state(STATE)

            if "reply" not in mode_to_include(STATE["mode"]) and newest["kind"] == "reply":
                return

            include = mode_to_include(STATE["mode"])
            current_count = sum(1 for x in visible if x["kind"] in include)

            text = (
                f"New Elon Musk post\n\n"
                f"time ET: {newest['local_dt'].strftime('%Y-%m-%d %H:%M')}\n"
                f"type: {newest['kind']}\n"
                f"current count (last 24h, mode={STATE['mode']}): {current_count}\n"
            )
            if newest.get("url"):
                text += f"\n{newest['url']}"

            chat_id = context.bot_data.get("watch_chat_id")
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=text)

    except Exception:
        pass


async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["watch_chat_id"] = update.effective_chat.id


async def wrapper(cmd_func, update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    await cmd_func(update, context)


def main():
    if "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Paste your real BOT_TOKEN into BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", lambda u, c: wrapper(cmd_start, u, c)))
    app.add_handler(CommandHandler("help", lambda u, c: wrapper(cmd_help, u, c)))
    app.add_handler(CommandHandler("status", lambda u, c: wrapper(cmd_status, u, c)))
    app.add_handler(CommandHandler("range", lambda u, c: wrapper(cmd_range, u, c)))
    app.add_handler(CommandHandler("today", lambda u, c: wrapper(cmd_today, u, c)))
    app.add_handler(CommandHandler("debugrange", lambda u, c: wrapper(cmd_debugrange, u, c)))
    app.add_handler(CommandHandler("setmode", lambda u, c: wrapper(cmd_setmode, u, c)))
    app.add_handler(CommandHandler("watch_on", lambda u, c: wrapper(cmd_watch_on, u, c)))
    app.add_handler(CommandHandler("watch_off", lambda u, c: wrapper(cmd_watch_off, u, c)))

    job_queue = app.job_queue
    job_queue.run_repeating(watcher, interval=WATCH_INTERVAL, first=15)

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
