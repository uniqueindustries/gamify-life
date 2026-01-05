import streamlit as st
import pandas as pd
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import math

# -----------------------------
# Config
# -----------------------------
APP_TITLE = "Gym RPG — Daily Attendance"
TZ = ZoneInfo("Australia/Sydney")
DB_PATH = "gym_rpg.db"

st.set_page_config(page_title=APP_TITLE, page_icon="🏋️", layout="centered")

# -----------------------------
# Database
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            intensity TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            notes TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

def meta_get(key: str, default: str) -> str:
    conn = db()
    cur = conn.execute("SELECT v FROM meta WHERE k = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def meta_set(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
    conn.commit()
    conn.close()

def read_checkins() -> pd.DataFrame:
    conn = db()
    df = pd.read_sql_query("SELECT day, created_at, intensity, minutes, notes FROM checkins ORDER BY day ASC", conn)
    conn.close()
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["created_at"] = pd.to_datetime(df["created_at"])
    return df

def has_checkin(day: date) -> bool:
    conn = db()
    cur = conn.execute("SELECT 1 FROM checkins WHERE day = ? LIMIT 1", (day.isoformat(),))
    row = cur.fetchone()
    conn.close()
    return row is not None

def add_checkin(day: date, intensity: str, minutes: int, notes: str):
    conn = db()
    conn.execute(
        "INSERT INTO checkins (day, created_at, intensity, minutes, notes) VALUES (?, ?, ?, ?, ?)",
        (day.isoformat(), datetime.now(TZ).isoformat(), intensity, minutes, notes.strip() if notes else None),
    )
    conn.commit()
    conn.close()

def delete_checkin(day: date):
    conn = db()
    conn.execute("DELETE FROM checkins WHERE day = ?", (day.isoformat(),))
    conn.commit()
    conn.close()

# -----------------------------
# Game Logic
# -----------------------------
INTENSITY_XP = {
    "Minimum (showed up)": 20,     # 10–20 minutes, mobility, light cardio etc.
    "Standard": 50,                # normal session
    "Hard": 80,                    # tough session
    "Recovery / Mobility": 25,     # deliberate recovery
}

def minutes_bonus(minutes: int) -> int:
    # small bonus that doesn't punish short sessions
    # caps at +30
    return min(30, max(0, int(minutes // 10) * 3))

def streak_bonus(streak: int) -> int:
    # gentle compounding, capped so it doesn't get silly
    return min(40, int(math.log2(streak + 1) * 10))

def level_from_xp(xp: int) -> int:
    # quadratic-ish curve: level grows slower over time
    # L1 at 0xp, L2 ~200, L3 ~450, L4 ~750...
    level = 1
    while xp >= xp_needed_for_level(level + 1):
        level += 1
    return level

def xp_needed_for_level(level: int) -> int:
    # total XP required to reach this level
    # tweakable
    return int(150 * (level - 1) ** 2 + 200 * (level - 1))

def progress_to_next_level(xp: int):
    lvl = level_from_xp(xp)
    lo = xp_needed_for_level(lvl)
    hi = xp_needed_for_level(lvl + 1)
    return lvl, lo, hi, (xp - lo) / max(1, (hi - lo))

def compute_streak(days: set[date], today: date) -> int:
    # streak ends at today if checked in today, otherwise ends at yesterday
    anchor = today if today in days else (today - timedelta(days=1))
    streak = 0
    d = anchor
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    return streak

@dataclass
class Achievement:
    key: str
    title: str
    desc: str
    predicate: callable  # (df, xp, streak) -> bool

def achievements():
    return [
        Achievement("first_blood", "First Check-in", "You showed up once. Identity begins.", lambda df, xp, streak: len(df) >= 1),
        Achievement("streak_7", "7-Day Streak", "Seven days. Most people quit before this.", lambda df, xp, streak: streak >= 7),
        Achievement("streak_30", "30-Day Streak", "A real system has formed.", lambda df, xp, streak: streak >= 30),
        Achievement("month_14", "14 This Month", "You trained 14+ days in the current month.", lambda df, xp, streak: month_count(df) >= 14),
        Achievement("month_25", "25 This Month", "Relentless month. Serious momentum.", lambda df, xp, streak: month_count(df) >= 25),
        Achievement("xp_1000", "1,000 XP", "You’re not ‘trying’ anymore. You’re doing.", lambda df, xp, streak: xp >= 1000),
        Achievement("xp_5000", "5,000 XP", "You’ve built a machine.", lambda df, xp, streak: xp >= 5000),
    ]

def month_count(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    now = datetime.now(TZ).date()
    start = now.replace(day=1)
    return int((df["day"] >= start).sum())

def compute_xp(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    total = 0
    days_set = set(df["day"].tolist())
    today = datetime.now(TZ).date()

    # compute per-checkin xp using current streak at that time (approx using rolling)
    # simple approach: compute current overall streak bonus once
    current_streak = compute_streak(days_set, today)

    for _, r in df.iterrows():
        base = INTENSITY_XP.get(r["intensity"], 30)
        total += base + minutes_bonus(int(r["minutes"]))
    # add a global streak bonus (keeps it simple)
    total += streak_bonus(current_streak)
    return int(total)

def quest_status(df: pd.DataFrame):
    # small “quests” that refresh daily/monthly
    today = datetime.now(TZ).date()
    days = set(df["day"].tolist()) if not df.empty else set()

    # Weekly quest: 5 check-ins in last 7 days
    last7_start = today - timedelta(days=6)
    last7 = df[(df["day"] >= last7_start) & (df["day"] <= today)] if not df.empty else df
    q_week = (len(last7), 5)

    # Monthly quest: 20 check-ins this month
    start_month = today.replace(day=1)
    month = df[df["day"] >= start_month] if not df.empty else df
    q_month = (len(month), 20)

    # Streak quest: 14-day streak
    streak = compute_streak(days, today)
    q_streak = (streak, 14)

    return {
        "Weekly: 5 in 7 days": q_week,
        "Monthly: 20 this month": q_month,
        "Streak: 14 days": q_streak,
    }

# -----------------------------
# UI Helpers
# -----------------------------
def header_card(name: str, streak: int, xp: int):
    lvl, lo, hi, p = progress_to_next_level(xp)
    st.title("🏋️ Gym RPG")
    st.caption("Attendance is sacred. Intensity is flexible. The chain does not break.")

    col1, col2, col3 = st.columns(3)
    col1.metric("🔥 Streak", f"{streak} days")
    col2.metric("⭐ XP", f"{xp}")
    col3.metric("🎮 Level", f"{lvl}")

    st.progress(min(1.0, max(0.0, p)))
    st.write(f"Next level at **{hi} XP** (you’re at **{xp}**)")

def github_heatmap(df: pd.DataFrame):
    st.subheader("🗓️ Consistency Map")
    if df.empty:
        st.info("No check-ins yet. Your first one starts the chain.")
        return

    # last 16 weeks (approx GitHub view)
    today = datetime.now(TZ).date()
    start = today - timedelta(days=7 * 16 - 1)
    days = pd.date_range(start, today, freq="D").date

    counts = {d: 0 for d in days}
    for d in df["day"].tolist():
        if d in counts:
            counts[d] += 1

    heat = pd.DataFrame({"day": list(counts.keys()), "count": list(counts.values())})
    heat["dow"] = pd.to_datetime(heat["day"]).dt.day_name()
    heat["week"] = (pd.to_datetime(heat["day"]) - pd.to_datetime(start)).dt.days // 7

    # Map day_name to order
    order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    heat["dow"] = pd.Categorical(heat["dow"], categories=order, ordered=True)
    heat = heat.sort_values(["week", "dow"])

    # Render as simple table grid using markdown (no external viz libs)
    # Build a 7 x N grid
    weeks = int(heat["week"].max()) + 1
    grid = [[0 for _ in range(weeks)] for _ in range(7)]
    dow_to_row = {name: i for i, name in enumerate(order)}

    for _, r in heat.iterrows():
        row = dow_to_row[str(r["dow"])]
        col = int(r["week"])
        grid[row][col] = int(r["count"])

    # Emoji scale (0..3)
    def cell(v):
        if v <= 0: return "⬛"
        if v == 1: return "🟩"
        if v == 2: return "🟦"
        return "🟪"

    lines = []
    for i, name in enumerate(order):
        lines.append(f"**{name[:3]}** " + " ".join(cell(v) for v in grid[i]))

    st.markdown("\n\n".join(lines))
    st.caption("⬛ none · 🟩 1 · 🟦 2 · 🟪 3+ (same day double logs are blocked)")

def achievements_panel(df: pd.DataFrame, xp: int, streak: int):
    st.subheader("🏅 Achievements")
    unlocked = []
    locked = []

    for a in achievements():
        ok = a.predicate(df, xp, streak)
        (unlocked if ok else locked).append(a)

    if unlocked:
        for a in unlocked:
            st.success(f"**{a.title}** — {a.desc}")
    else:
        st.info("No achievements yet. First check-in unlocks the first badge.")

    if locked:
        with st.expander("Locked achievements"):
            for a in locked:
                st.write(f"🔒 **{a.title}** — {a.desc}")

def quests_panel(df: pd.DataFrame):
    st.subheader("🧩 Quests")
    qs = quest_status(df)
    for k, (cur, target) in qs.items():
        st.write(f"**{k}**")
        st.progress(min(1.0, cur / target))
        st.caption(f"{cur} / {target}")

def log_panel(df: pd.DataFrame):
    st.subheader("✅ Daily Check-in")

    today = datetime.now(TZ).date()
    already = has_checkin(today)

    with st.form("checkin_form", clear_on_submit=False):
        intensity = st.selectbox("Session type", list(INTENSITY_XP.keys()), index=1)
        minutes = st.slider("Minutes (roughly)", 5, 120, 35, step=5)
        notes = st.text_input("Notes (optional)", placeholder="e.g., late night = still showed up")

        submitted = st.form_submit_button("Log today's gym visit")

    if submitted:
        if already:
            st.warning("Today is already logged. The chain is protected.")
        else:
            add_checkin(today, intensity, int(minutes), notes or "")
            st.success("Logged. Attendance secured.")
            st.rerun()

    colA, colB = st.columns(2)
    with colA:
        st.write(f"Today: **{today.isoformat()}**")
        st.write("Status: " + ("✅ Logged" if already else "⏳ Not logged yet"))
    with colB:
        if already and st.button("Undo today's check-in"):
            delete_checkin(today)
            st.info("Undid today. (Use this only for accidental logs.)")
            st.rerun()

def history_panel(df: pd.DataFrame):
    st.subheader("📜 History")
    if df.empty:
        return

    show = df.copy()
    show["day"] = show["day"].astype(str)
    show["minutes"] = show["minutes"].astype(int)
    show = show.sort_values("day", ascending=False)

    st.dataframe(show, use_container_width=True, hide_index=True)

# -----------------------------
# Main
# -----------------------------
def main():
    init_db()

    # Profile name (simple personalization)
    if "player_name" not in st.session_state:
        st.session_state.player_name = meta_get("player_name", "Player")

    with st.sidebar:
        st.header("⚙️ Settings")
        name = st.text_input("Name", value=st.session_state.player_name)
        if st.button("Save"):
            st.session_state.player_name = name.strip() or "Player"
            meta_set("player_name", st.session_state.player_name)
            st.success("Saved.")

        st.divider()
        st.write("**Rule:** You go every day. The workout can vary. Attendance does not.")
        st.caption("Tip: On chaos days, log a Minimum or Mobility session. Keep the chain.")

    df = read_checkins()
    days_set = set(df["day"].tolist()) if not df.empty else set()
    today = datetime.now(TZ).date()

    streak = compute_streak(days_set, today)
    xp = compute_xp(df)

    header_card(st.session_state.player_name, streak, xp)

    st.divider()
    log_panel(df)

    st.divider()
    quests_panel(df)

    st.divider()
    achievements_panel(df, xp, streak)

    st.divider()
    github_heatmap(df)

    st.divider()
    history_panel(df)

    st.divider()
    st.subheader("🧠 The Contract")
    st.markdown(
        """
- **Attendance is sacred.** You enter the gym every day.
- **Intensity is flexible.** Minimum sessions are allowed (and powerful).
- **No “make-up days.”** Today is the only day that exists.
        """.strip()
    )

if __name__ == "__main__":
    main()
