import os
import sqlite3
import random
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from myserver import keep_alive
keep_alive()

# =========================
# OPTIONAL: .env support (local dev)
# =========================
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =========================
# CONFIG
# =========================
DB_PATH = "points.db"
TZ = ZoneInfo("Asia/Bangkok")

DAILY_AMOUNT = 10
ROLL_COST = 10

# Rewards: (name, weight)
REWARDS = [
    ("เสียใจด้วยไม่ได้รางวัล 😭", 60),
    ("เงินเขียว 5,000 🟩", 25),
    ("เงินเขียว 10,000 🟩", 10),
    ("เงินแดง 3,000 🟥", 4),
    ("สกินไม้สุดแรร์ 🌟", 1),
]

# Voice reward config
VOICE_REWARD_INTERVAL_SEC = 60 * 60   # 1 hour
VOICE_REWARD_POINTS = 10
VOICE_CHECK_EVERY_SEC = 60            # check every 60s
VOICE_MUTE_LIMIT_SEC = 30 * 60        # 30 min muted -> reset current accum to 0

# Settings keys (per guild)
KEY_DAILY_CHANNEL = "daily_channel_id"
KEY_GACHA_CHANNEL = "gacha_channel_id"
KEY_VOICE_CHANNELS = "voice_channel_ids"  # comma-separated voice channel ids
KEY_LOG_CHANNEL = "log_channel_id"

# Persistent button IDs (must be stable)
BTN_DAILY_ID = "aurapoint_daily_claim"
BTN_GACHA_ID = "aurapoint_gacha_roll"
BTN_CHECK_ID = "aurapoint_check_points"

# =========================
# DB
# =========================
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (guild_id, key)
        )
        """)
        con.commit()


def get_setting(guild_id: int, key: str):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?",
            (guild_id, key)
        ).fetchone()
        return row[0] if row else None


def set_setting(guild_id: int, key: str, value: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, value)
        )
        con.commit()


def ensure_user(con: sqlite3.Connection, guild_id: int, user_id: int):
    row = con.execute(
        "SELECT points, last_daily FROM users WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()
    if row:
        return row[0], row[1]
    con.execute(
        "INSERT INTO users (guild_id, user_id, points, last_daily) VALUES (?, ?, 0, NULL)",
        (guild_id, user_id)
    )
    con.commit()
    return 0, None


def get_points(guild_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        points, _ = ensure_user(con, guild_id, user_id)
        return points


def add_points(guild_id: int, user_id: int, amount: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        points, _ = ensure_user(con, guild_id, user_id)
        points += amount
        con.execute(
            "UPDATE users SET points=? WHERE guild_id=? AND user_id=?",
            (points, guild_id, user_id)
        )
        con.commit()
        return points


def set_points(guild_id: int, user_id: int, points: int):
    with sqlite3.connect(DB_PATH) as con:
        ensure_user(con, guild_id, user_id)
        con.execute(
            "UPDATE users SET points=? WHERE guild_id=? AND user_id=?",
            (points, guild_id, user_id)
        )
        con.commit()


def get_last_daily(guild_id: int, user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        _, last_daily = ensure_user(con, guild_id, user_id)
        return last_daily


def set_last_daily(guild_id: int, user_id: int, last_daily: str):
    with sqlite3.connect(DB_PATH) as con:
        ensure_user(con, guild_id, user_id)
        con.execute(
            "UPDATE users SET last_daily=? WHERE guild_id=? AND user_id=?",
            (last_daily, guild_id, user_id)
        )
        con.commit()


# =========================
# VOICE CHANNELS (multi)
# =========================
def get_voice_channel_ids(guild_id: int) -> list[int]:
    raw = get_setting(guild_id, KEY_VOICE_CHANNELS)
    if not raw:
        return []
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return sorted(set(ids))


def save_voice_channel_ids(guild_id: int, ids: list[int]):
    ids = sorted(set(ids))
    set_setting(guild_id, KEY_VOICE_CHANNELS, ",".join(map(str, ids)))


def add_voice_channel_id(guild_id: int, channel_id: int) -> bool:
    ids = get_voice_channel_ids(guild_id)
    if channel_id in ids:
        return False
    ids.append(channel_id)
    save_voice_channel_ids(guild_id, ids)
    return True


def remove_voice_channel_id(guild_id: int, channel_id: int) -> bool:
    ids = get_voice_channel_ids(guild_id)
    if channel_id not in ids:
        return False
    ids.remove(channel_id)
    save_voice_channel_ids(guild_id, ids)
    return True


# =========================
# REWARDS
# =========================
def roll_reward() -> str:
    total = sum(w for _, w in REWARDS)
    r = random.uniform(0, total)
    upto = 0
    for name, weight in REWARDS:
        upto += weight
        if upto >= r:
            return name
    return REWARDS[-1][0]


# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# LOGGING
# =========================
async def send_log(guild: discord.Guild, text: str = None, embed: discord.Embed = None):
    if not guild:
        return
    log_ch_id = get_setting(guild.id, KEY_LOG_CHANNEL)
    if not log_ch_id:
        return

    ch = guild.get_channel(int(log_ch_id))
    if not ch:
        return

    try:
        if embed:
            await ch.send(embed=embed)
        else:
            await ch.send(text or "")
    except Exception:
        pass


# =========================
# UI VIEWS (Persistent)
# =========================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="รับ Daily +10", style=discord.ButtonStyle.success, custom_id=BTN_DAILY_ID)
    async def daily_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.guild_id:
            return await interaction.response.send_message("ใช้คำสั่งนี้ในเซิร์ฟก่อนนะ", ephemeral=True)

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        daily_ch = get_setting(guild_id, KEY_DAILY_CHANNEL)
        if daily_ch and str(interaction.channel_id) != str(daily_ch):
            return await interaction.response.send_message("❌ ปุ่ม Daily ใช้ได้เฉพาะห้องที่ตั้งไว้เท่านั้น", ephemeral=True)

        today_str = datetime.now(TZ).date().isoformat()
        last = get_last_daily(guild_id, user_id)

        if last == today_str:
            return await interaction.response.send_message("⛔ วันนี้รับไปแล้วนะ พรุ่งนี้ค่อยมากดใหม่ 💜", ephemeral=True)

        before_pts = get_points(guild_id, user_id)
        new_points = add_points(guild_id, user_id, DAILY_AMOUNT)
        set_last_daily(guild_id, user_id, today_str)

        # player ephemeral
        await interaction.response.send_message(
            f"✅ รับ Daily +{DAILY_AMOUNT} แต้มแล้ว!\nแต้มคงเหลือ: **{new_points}**",
            ephemeral=True
        )

        # admin log (optional)
        emb = discord.Embed(title="✅ DAILY CLAIM", color=0x2ECC71)
        emb.add_field(name="ผู้กด", value=f"<@{interaction.user.id}>", inline=False)
        emb.add_field(name="ก่อนกด", value=str(before_pts), inline=True)
        emb.add_field(name="หลังกด", value=str(new_points), inline=True)
        emb.set_footer(text=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        await send_log(interaction.guild, embed=emb)


class GachaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=f"สุ่มรางวัล (-{ROLL_COST} แต้ม)", style=discord.ButtonStyle.primary, custom_id=BTN_GACHA_ID)
    async def gacha_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.guild_id:
            return await interaction.response.send_message("ใช้คำสั่งนี้ในเซิร์ฟก่อนนะ", ephemeral=True)

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        gacha_ch = get_setting(guild_id, KEY_GACHA_CHANNEL)
        if gacha_ch and str(interaction.channel_id) != str(gacha_ch):
            return await interaction.response.send_message("❌ ปุ่มสุ่มรางวัลใช้ได้เฉพาะห้องที่ตั้งไว้เท่านั้น", ephemeral=True)

        pts_before = get_points(guild_id, user_id)
        if pts_before < ROLL_COST:
            return await interaction.response.send_message(
                f"❌ แต้มไม่พอ! ต้องใช้ {ROLL_COST} แต้ม (ตอนนี้มี {pts_before})",
                ephemeral=True
            )

        # Deduct then roll
        set_points(guild_id, user_id, pts_before - ROLL_COST)
        reward = roll_reward()
        pts_after = get_points(guild_id, user_id)

        # player ephemeral (เดิมดีแล้ว)
        await interaction.response.send_message(
            f"🎲 ผลสุ่มของคุณ: **{reward}**\nแต้มคงเหลือ: **{pts_after}**",
            ephemeral=True
        )

        # admin log (ตามที่ฟุขอ: แค่ก่อน/หลัง/ได้อะไร + ผู้กดเป็น @id)
        emb = discord.Embed(title="🎲 GACHA LOG", color=0x3498DB)
        emb.add_field(name="ผู้กด", value=f"<@{interaction.user.id}>", inline=False)
        emb.add_field(name="แต้มก่อนกด", value=str(pts_before), inline=True)
        emb.add_field(name="แต้มหลังกด", value=str(pts_after), inline=True)
        emb.add_field(name="รางวัลที่ได้", value=reward, inline=False)
        emb.set_footer(text=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        await send_log(interaction.guild, embed=emb)

    @discord.ui.button(label="เช็คคะแนน", style=discord.ButtonStyle.secondary, custom_id=BTN_CHECK_ID)
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.guild_id:
            return await interaction.response.send_message("ใช้คำสั่งนี้ในเซิร์ฟก่อนนะ", ephemeral=True)

        guild_id = interaction.guild_id
        pts = get_points(guild_id, interaction.user.id)
        await interaction.response.send_message(f"📊 แต้มของคุณตอนนี้: **{pts}**", ephemeral=True)


# =========================
# COMMANDS
# =========================
@bot.command()
async def points(ctx: commands.Context):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    pts = get_points(ctx.guild.id, ctx.author.id)
    await ctx.send(f"📊 {ctx.author.mention} มีแต้ม: **{pts}**")


@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx: commands.Context, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    if channel is None:
        channel = ctx.channel

    set_setting(ctx.guild.id, KEY_DAILY_CHANNEL, str(channel.id))

    embed = discord.Embed(
        title="AURA DAILY POINT",
        description=f"กดรับแต้มรายวันได้วันละ 1 ครั้ง (+{DAILY_AMOUNT} แต้ม)\nผลตอบกลับเห็นเฉพาะคนกด (ไม่รกห้อง)",
        color=0x9B59B6
    )

    await channel.send(embed=embed, view=DailyView())
    await ctx.send(f"✅ ตั้งปุ่ม Daily ที่ {channel.mention} แล้ว")


@bot.command()
@commands.has_permissions(administrator=True)
async def setupgacha(ctx: commands.Context, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    if channel is None:
        channel = ctx.channel

    set_setting(ctx.guild.id, KEY_GACHA_CHANNEL, str(channel.id))

    reward_lines = "\n".join([f"• {name} (weight {w})" for name, w in REWARDS])
    embed = discord.Embed(
        title="AURA GACHA",
        description=f"กดสุ่มรางวัล: ใช้ **{ROLL_COST}** แต้ม/ครั้ง\nผลสุ่มเป็น Ephemeral (เห็นเฉพาะคนกด)\n\n**รายการรางวัล:**\n{reward_lines}",
        color=0x3498DB
    )

    await channel.send(embed=embed, view=GachaView())
    await ctx.send(f"✅ ตั้งปุ่ม Gacha ที่ {channel.mention} แล้ว")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogchannel(ctx: commands.Context, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    if channel is None:
        channel = ctx.channel

    set_setting(ctx.guild.id, KEY_LOG_CHANNEL, str(channel.id))
    await ctx.send(f"✅ ตั้งห้อง Logs เป็น {channel.mention} แล้ว")


@bot.command()
@commands.has_permissions(administrator=True)
async def addvoicechannel(ctx: commands.Context, channel: discord.VoiceChannel = None):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    if channel is None:
        return await ctx.send("❌ ใช้แบบนี้: `!addvoicechannel #ชื่อห้องเสียง`")

    ok = add_voice_channel_id(ctx.guild.id, channel.id)
    if ok:
        await ctx.send(f"✅ เพิ่มห้องเสียงรับแต้ม: {channel.mention}")
    else:
        await ctx.send(f"ℹ️ ห้องนี้อยู่ในลิสต์แล้ว: {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def removevoicechannel(ctx: commands.Context, channel: discord.VoiceChannel = None):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")
    if channel is None:
        return await ctx.send("❌ ใช้แบบนี้: `!removevoicechannel #ชื่อห้องเสียง`")

    ok = remove_voice_channel_id(ctx.guild.id, channel.id)
    if ok:
        await ctx.send(f"🗑️ ลบห้องเสียงรับแต้ม: {channel.mention}")
    else:
        await ctx.send(f"ℹ️ ห้องนี้ไม่ได้อยู่ในลิสต์: {channel.mention}")


@bot.command()
async def listvoicechannels(ctx: commands.Context):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")

    ids = get_voice_channel_ids(ctx.guild.id)
    if not ids:
        return await ctx.send("📌 ยังไม่ได้ตั้งห้องเสียงรับแต้ม\nใช้ `!addvoicechannel #ห้องเสียง`")

    lines = []
    for cid in ids:
        ch = ctx.guild.get_channel(cid)
        lines.append(ch.mention if ch else f"`{cid}` (หาไม่เจอ)")
    await ctx.send("🎙 ห้องเสียงที่รับแต้มตอนนี้:\n" + "\n".join(f"• {x}" for x in lines))


@bot.command()
@commands.has_permissions(administrator=True)
async def showsettings(ctx: commands.Context):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟก่อนนะ")

    g = ctx.guild.id
    daily = get_setting(g, KEY_DAILY_CHANNEL)
    gacha = get_setting(g, KEY_GACHA_CHANNEL)
    logch = get_setting(g, KEY_LOG_CHANNEL)
    voice_ids = get_voice_channel_ids(g)

    def ch_mention(cid):
        if not cid:
            return "ยังไม่ตั้ง"
        return f"<#{cid}>"

    voice_str = "ยังไม่ตั้ง"
    if voice_ids:
        voice_str = "\n".join([f"• <#{cid}>" for cid in voice_ids])

    await ctx.send(
        "⚙️ **Settings ของเซิร์ฟนี้**\n"
        f"Daily Channel: {ch_mention(daily)}\n"
        f"Gacha Channel: {ch_mention(gacha)}\n"
        f"Log Channel: {ch_mention(logch)}\n"
        f"Voice Channels:\n{voice_str}"
    )


# =========================
# VOICE REWARD LOOP
# =========================
# progress[(guild_id, user_id)] = {"accum": seconds, "muted": seconds}
voice_progress = {}


def is_muted(member: discord.Member) -> bool:
    if not member.voice:
        return True
    v = member.voice
    return bool(v.self_mute or v.self_deaf or v.mute or v.deaf)


async def safe_dm(member: discord.Member, text: str):
    try:
        await member.send(text)
    except Exception:
        pass


@tasks.loop(seconds=VOICE_CHECK_EVERY_SEC)
async def voice_reward_loop():
    seen = set()  # (guild_id, user_id)

    for guild in bot.guilds:
        allowed_ids = get_voice_channel_ids(guild.id)
        if not allowed_ids:
            continue

        for vc_id in allowed_ids:
            channel = guild.get_channel(int(vc_id))
            if channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue

            for member in list(channel.members):
                if member.bot:
                    continue

                key = (guild.id, member.id)
                seen.add(key)

                state = voice_progress.get(key, {"accum": 0, "muted": 0})

                if is_muted(member):
                    state["muted"] += VOICE_CHECK_EVERY_SEC
                    if state["muted"] >= VOICE_MUTE_LIMIT_SEC:
                        state["accum"] = 0
                    voice_progress[key] = state
                    continue

                # not muted
                state["muted"] = 0
                state["accum"] += VOICE_CHECK_EVERY_SEC

                # award for each full interval
                while state["accum"] >= VOICE_REWARD_INTERVAL_SEC:
                    state["accum"] -= VOICE_REWARD_INTERVAL_SEC
                    before_pts = get_points(guild.id, member.id)
                    new_pts = add_points(guild.id, member.id, VOICE_REWARD_POINTS)

                    await safe_dm(
                        member,
                        f"🎧 คุณอยู่ห้องเสียงครบ 1 ชั่วโมง ได้รับ +{VOICE_REWARD_POINTS} แต้ม!\n"
                        f"แต้มในเซิร์ฟ **{guild.name}** ตอนนี้: {new_pts}"
                    )

                    # optional admin log
                    emb = discord.Embed(title="🎧 VOICE REWARD", color=0x9B59B6)
                    emb.add_field(name="ผู้ได้รับ", value=f"<@{member.id}>", inline=False)
                    emb.add_field(name="ก่อนรับ", value=str(before_pts), inline=True)
                    emb.add_field(name="หลังรับ", value=str(new_pts), inline=True)
                    emb.set_footer(text=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
                    await send_log(guild, embed=emb)

                voice_progress[key] = state

    # Remove progress for users not currently in allowed channels
    for key in list(voice_progress.keys()):
        if key not in seen:
            del voice_progress[key]


@voice_reward_loop.before_loop
async def before_voice_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(2)


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    init_db()

    # Register persistent views so buttons keep working after restart
    bot.add_view(DailyView())
    bot.add_view(GachaView())

    if not voice_reward_loop.is_running():
        voice_reward_loop.start()

    print(f"Logged in as {bot.user}")


# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(TOKEN)
