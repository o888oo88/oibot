import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import matplotlib.pyplot as plt

from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"

BASE = "https://xtracker.polymarket.com/api"
KINDS = ("original", "reply", "repost", "quote")

DEFAULT_USER = "elonmusk"
DEFAULT_PLATFORM = "X"
DEFAULT_TZ = "America/New_York"

STATE_FILE = Path("bot_state.json")
OUT_DIR = Path("tg_reports")
OUT_DIR.mkdir(exist_ok=True)

DEFAULT_MODE = "pm"
WATCH_INTERVAL = 60


# =========================
# STATE
# =========================
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


# =========================
# EXACT ENGINE
# =========================
def parse_local(s: str, tz: ZoneInfo) -> datetime:
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=tz)


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return to_utc(dt).isoformat()


def get_posts(handle: str, start_utc: datetime, end_utc: datetime, platform: str = "X") -> list[dict]:
    params = {"platform": platform, "startDate": iso(start_utc), "endDate": iso(end_utc)}
    r = requests.get(f"{BASE}/users/{handle}/posts", params=params, timeout=60)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(j)
    return j["data"]


def pick_ts(p: dict) -> datetime | None:
    for key in ("createdAt", "created_at", "timestamp", "created", "date"):
        v = p.get(key)
        if not v:
            continue
        try:
            if isinstance(v, str):
                if v.endswith("Z"):
                    v = v[:-1] + "+00:00"
                dt = datetime.fromisoformat(v)
            else:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def classify(p: dict) -> str:
    t = (p.get("type") or p.get("postType") or p.get("kind") or "").lower()
    if t == "retweet":
        return "repost"
    if t == "tweet":
        return "original"
    if t in KINDS:
        return t

    if p.get("isReply") is True:
        return "reply"
    if p.get("isRepost") is True or p.get("isRetweet") is True:
        return "repost"
    if p.get("isQuote") is True:
        return "quote"

    for k in ("inReplyToTweetId", "in_reply_to_tweet_id", "inReplyToId", "replyToId"):
        if p.get(k):
            return "reply"
    for k in ("repostedTweetId", "retweetedTweetId", "retweetId", "repostId"):
        if p.get(k):
            return "repost"
    for k in ("quotedTweetId", "quoteTweetId", "quoteId"):
        if p.get(k):
            return "quote"

    return "original"


def mode_to_include(mode: str) -> set[str]:
    mode = mode.lower()
    if mode == "pm":
        return {"original", "quote", "repost"}
    if mode == "all":
        return {"original", "reply", "repost", "quote"}
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


# =========================
# REPORT
# =========================
def build_report_exact(start_local: datetime, end_local: datetime, mode: str):
    tz = ZoneInfo(DEFAULT_TZ)
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)
    include_set = mode_to_include(mode)

    posts = get_posts(DEFAULT_USER, start_utc, end_utc, platform=DEFAULT_PLATFORM)

    rows = []
    filtered = []

    for p in posts:
        ts = pick_ts(p)
        if not ts:
            continue
        if not (start_utc <= ts <= end_utc):
            continue

        kind = classify(p)
        ts_local = ts.astimezone(tz)

        rows.append({
            "date_et": ts_local.date().isoformat(),
            "hour_et": f"{ts_local.hour:02d}",
            "kind": kind,
        })

        filtered.append({
            "id": p.get("id"),
            "url": p.get("url") or p.get("link"),
            "createdAt_utc": ts.isoformat(),
            "createdAt_local": ts_local.isoformat(),
            "kind": kind,
            "content": (p.get("content") or p.get("text") or "").strip(),
            "raw": p,
        })

    df = pd.DataFrame(rows)

    if df.empty:
        return {
            "start_local": start_local,
            "end_local": end_local,
            "mode": mode,
            "total_pm": 0,
            "type_totals": {"original": 0, "reply": 0, "repost": 0, "quote": 0},
            "daily_df": pd.DataFrame(columns=["date_et", "original", "reply", "repost", "quote", "all", "pm_total"]),
            "csv_path": None,
            "png_path": None,
            "dump_path": None,
            "filtered": [],
        }

    # Daily
    daily = df.pivot_table(index="date_et", columns="kind", aggfunc="size", fill_value=0)
    for k in KINDS:
        if k not in daily.columns:
            daily[k] = 0
    daily = daily[list(KINDS)]
    daily["all"] = daily.sum(axis=1)
    daily["pm_total"] = 0
    for k in include_set:
        daily["pm_total"] += daily[k]
    daily_df = daily.reset_index()

    total_pm = int(daily_df["pm_total"].sum())
    type_totals = {
        "original": int(daily_df["original"].sum()),
        "reply": int(daily_df["reply"].sum()),
        "repost": int(daily_df["repost"].sum()),
        "quote": int(daily_df["quote"].sum()),
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"report_{timestamp}.csv"
    png_path = OUT_DIR / f"heatmap_{timestamp}.png"
    dump_path = OUT_DIR / f"dump_{timestamp}.json"

    daily_df.to_csv(csv_path, index=False, encoding="utf-8")

    with open(dump_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)

    # Heatmap with TOTAL column
    heat_raw = df[df["kind"].isin(include_set)].copy()
    heat = heat_raw.pivot_table(index="date_et", columns="hour_et", aggfunc="size", fill_value=0)

    all_days = sorted(daily_df["date_et"].tolist())
    all_hours_cols = [f"{h:02d}" for h in range(24)]
    heat = heat.reindex(index=all_days, columns=all_hours_cols, fill_value=0)

    heat["TOTAL"] = heat.sum(axis=1)

    plt.figure(figsize=(18, max(4, len(heat.index) * 0.5)))
    plt.imshow(heat.values, aspect="auto")
    plt.colorbar(label="Posts per hour / total")

    x_labels = all_hours_cols + ["TOTAL"]
    plt.xticks(range(len(x_labels)), x_labels)
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
        "total_pm": total_pm,
        "type_totals": type_totals,
        "daily_df": daily_df,
        "csv_path": csv_path,
        "png_path": png_path,
        "dump_path": dump_path,
        "filtered": filtered,
    }


# =========================
# TELEGRAM HELPERS
# =========================
def format_df_text(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df.empty:
        return "No data"
    return df.head(max_rows).to_string(index=False)


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

/range 2026-03-06 2026-03-13
/range 2026-03-06 12:00 2026-03-13 12:00

/today

/setmode pm
/setmode all
/setmode original

/watch_on
/watch_off
""".strip()


# =========================
# TIME WINDOWS
# =========================
def parse_range_args(args: list[str]):
    tz = ZoneInfo(DEFAULT_TZ)

    # date-only => Polymarket style 12:00 -> 12:00
    if len(args) == 2:
        start_local = parse_local(f"{args[0]} 12:00", tz)
        end_local = parse_local(f"{args[1]} 12:00", tz)
        return start_local, end_local

    # exact datetime
    if len(args) == 4:
        start_local = parse_local(f"{args[0]} {args[1]}", tz)
        end_local = parse_local(f"{args[2]} {args[3]}", tz)
        return start_local, end_local

    raise ValueError("Use /range YYYY-MM-DD YYYY-MM-DD or /range YYYY-MM-DD HH:MM YYYY-MM-DD HH:MM")


def get_today_window_pm_style() -> tuple[datetime, datetime]:
    tz = ZoneInfo(DEFAULT_TZ)
    now_local = datetime.now(tz).replace(second=0, microsecond=0)

    today_1200 = parse_local(now_local.strftime("%Y-%m-%d") + " 12:00", tz)

    if now_local >= today_1200:
        start_local = today_1200
    else:
        yesterday = now_local - timedelta(days=1)
        start_local = parse_local(yesterday.strftime("%Y-%m-%d") + " 12:00", tz)

    end_local = now_local
    return start_local, end_local


async def send_report(update: Update, report: dict):
    await update.message.reply_text(format_summary(report))

    daily_text = "Daily breakdown:\n\n" + format_df_text(report["daily_df"])
    await update.message.reply_text(f"<pre>{daily_text}</pre>", parse_mode="HTML")

    if report["csv_path"]:
        with open(report["csv_path"], "rb") as f:
            await update.message.reply_document(InputFile(f, filename=report["csv_path"].name))

    if report["png_path"]:
        with open(report["png_path"], "rb") as f:
            await update.message.reply_photo(InputFile(f, filename=report["png_path"].name))


# =========================
# COMMANDS
# =========================
async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["watch_chat_id"] = update.effective_chat.id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    text = (
        f"User: {DEFAULT_USER}\n"
        f"Mode: {mode_label(STATE['mode'])}\n"
        f"Timezone: {DEFAULT_TZ}\n"
        f"Watch: {'ON' if STATE['watch_enabled'] else 'OFF'}\n"
        f"Last seen post id: {STATE.get('last_seen_post_id')}"
    )
    await update.message.reply_text(text)


async def cmd_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    try:
        start_local, end_local = parse_range_args(context.args)
        report = build_report_exact(start_local, end_local, STATE["mode"])
        await send_report(update, report)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    try:
        start_local, end_local = get_today_window_pm_style()
        report = build_report_exact(start_local, end_local, STATE["mode"])
        await send_report(update, report)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_setmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
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
    await remember_chat(update, context)
    STATE["watch_enabled"] = True
    save_state(STATE)
    await update.message.reply_text("Watch enabled.")


async def cmd_watch_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat(update, context)
    STATE["watch_enabled"] = False
    save_state(STATE)
    await update.message.reply_text("Watch disabled.")


# =========================
# WATCHER
# =========================
async def watcher(context: ContextTypes.DEFAULT_TYPE):
    if not STATE.get("watch_enabled"):
        return

    try:
        tz = ZoneInfo(DEFAULT_TZ)
        now_local = datetime.now(tz)
        start_local = now_local - timedelta(hours=24)
        end_local = now_local

        report = build_report_exact(start_local, end_local, STATE["mode"])
        filtered = report["filtered"]
        if not filtered:
            return

        filtered.sort(key=lambda x: x["createdAt_local"])
        newest = filtered[-1]
        newest_id = newest["id"]

        if newest_id and newest_id != STATE.get("last_seen_post_id"):
            STATE["last_seen_post_id"] = newest_id
            save_state(STATE)

            chat_id = context.bot_data.get("watch_chat_id")
            if chat_id:
                text = (
                    f"New Elon Musk post\n\n"
                    f"time ET: {newest['createdAt_local']}\n"
                    f"type: {newest['kind']}\n"
                    f"current count (last 24h, mode={STATE['mode']}): {report['total_pm']}"
                )
                if newest.get("url"):
                    text += f"\n\n{newest['url']}"
                await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass


def main():
    if "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Paste your real BOT_TOKEN into BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("range", cmd_range))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("setmode", cmd_setmode))
    app.add_handler(CommandHandler("watch_on", cmd_watch_on))
    app.add_handler(CommandHandler("watch_off", cmd_watch_off))

    app.job_queue.run_repeating(watcher, interval=WATCH_INTERVAL, first=15)

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
