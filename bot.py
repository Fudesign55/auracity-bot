import os
import sqlite3
import random
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# =========================
# KEEP ALIVE (Flask) - Railway ใช้ได้ / Local ก็ใช้ได้
# =========================
from myserver import server_on

# =========================
# LOAD ENV
# =========================
load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ ไม่พบ DISCORD_TOKEN ในไฟล์ .env (ต้องชื่อ .env และอยู่โฟลเดอร์เดียวกับ bot.py)")

server_on()

# =========================
# PATH / TIMEZONE
# =========================
DB_PATH = str(Path(__file__).with_name("points.db"))
TH_TZ = ZoneInfo("Asia/Bangkok")

print("DB_PATH =", DB_PATH)

# =========================
# CONFIG (แก้ตรงนี้ได้)
# =========================
DAILY_AMOUNT = 10
ROLL_COST = 10

# Voice reward
VOICE_REWARD_MINUTES = 60        # อยู่ครบกี่นาที ได้แต้ม
VOICE_REWARD_POINTS = 10         # ได้กี่แต้มต่อรอบ
VOICE_CHECK_EVERY_MIN = 1        # เช็คทุกกี่นาที
VOICE_MUTE_LIMIT_MIN = 30        # mute เกินกี่นาที รีเซ็ต

# Embed Image (ใส่ลิงก์รูปได้)
DAILY_IMAGE_URL = ""  # เช่น "https://.../daily.png"
GACHA_IMAGE_URL = ""  # เช่น "https://.../gacha.png"

# =========================
# INTENTS
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# DB INIT
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

        con.execute("""
        CREATE TABLE IF NOT EXISTS voice_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS voice_progress (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            active_minutes INTEGER NOT NULL DEFAULT 0,
            muted_streak_minutes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, channel_id)
        )
        """)
        con.commit()


def set_setting(guild_id: int, key: str, value: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO settings (guild_id, key, value) VALUES (?, ?, ?)",
            (guild_id, key, value)
        )
        con.commit()


def get_setting(guild_id: int, key: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = cur.fetchone()
        return row[0] if row else None


def get_user(con: sqlite3.Connection, guild_id: int, user_id: int):
    cur = con.cursor()
    cur.execute(
        "SELECT points, last_daily FROM users WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    )
    row = cur.fetchone()
    if row:
        return row[0], row[1]

    cur.execute(
        "INSERT INTO users (guild_id, user_id, points, last_daily) VALUES (?, ?, 0, NULL)",
        (guild_id, user_id)
    )
    con.commit()
    return 0, None


def set_user_points(con: sqlite3.Connection, guild_id: int, user_id: int, points: int):
    con.execute(
        "UPDATE users SET points=? WHERE guild_id=? AND user_id=?",
        (points, guild_id, user_id)
    )
    con.commit()


def set_user_last_daily(con: sqlite3.Connection, guild_id: int, user_id: int, date_str: str):
    con.execute(
        "UPDATE users SET last_daily=? WHERE guild_id=? AND user_id=?",
        (date_str, guild_id, user_id)
    )
    con.commit()


def add_voice_channel(guild_id: int, channel_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO voice_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id)
        )
        con.commit()


def remove_voice_channel(guild_id: int, channel_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM voice_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        )
        con.execute(
            "DELETE FROM voice_progress WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        )
        con.commit()


def list_voice_channels(guild_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT channel_id FROM voice_channels WHERE guild_id=?", (guild_id,))
        return [r[0] for r in cur.fetchall()]


async def send_log(guild: discord.Guild, text: str):
    log_ch_id = get_setting(guild.id, "log_channel_id")
    if not log_ch_id:
        return
    ch = guild.get_channel(int(log_ch_id))
    if ch:
        await ch.send(text)


# =========================
# GACHA REWARDS (แก้ตรงนี้ได้)
# =========================
# rate = น้ำหนัก ยิ่งมากออกบ่อย
GACHA_REWARDS = [
    {"name": "🍀 โชคดีนิดๆ", "rate": 50},
    {"name": "💎 ของหายาก!", "rate": 15},
    {"name": "🔥 JACKPOT!!", "rate": 3},
    {"name": "😆 ได้แค่ลม", "rate": 32},
]

def roll_reward():
    total = sum(r["rate"] for r in GACHA_REWARDS)
    pick = random.randint(1, total)
    cur = 0
    for r in GACHA_REWARDS:
        cur += r["rate"]
        if pick <= cur:
            return r["name"]
    return GACHA_REWARDS[-1]["name"]


# =========================
# UI - DAILY
# =========================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=f"✅ รับ Daily +{DAILY_AMOUNT}", style=discord.ButtonStyle.success, custom_id="aura:daily")
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild_id:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        today = datetime.now(TH_TZ).strftime("%Y-%m-%d")

        with sqlite3.connect(DB_PATH) as con:
            points, last_daily = get_user(con, guild_id, user_id)

            if last_daily == today:
                await interaction.response.send_message(
                    f"วันนี้รับไปแล้วน้า 😝\nแต้มตอนนี้: **{points}**",
                    ephemeral=True
                )
                return

            before = points
            points += DAILY_AMOUNT
            set_user_points(con, guild_id, user_id, points)
            set_user_last_daily(con, guild_id, user_id, today)

        await interaction.response.send_message(
            f"รับ Daily แล้ว ✅ +{DAILY_AMOUNT}\nแต้ม: **{before} → {points}**",
            ephemeral=True
        )

        await send_log(
            interaction.guild,
            f"📌 DAILY | {interaction.user.mention} | {before} → {points}"
        )


# =========================
# UI - GACHA
# =========================
class GachaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=f"🎲 สุ่มรางวัล (เสีย {ROLL_COST} แต้ม)", style=discord.ButtonStyle.primary, custom_id="aura:gacha")
    async def gacha_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild_id:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        with sqlite3.connect(DB_PATH) as con:
            points, _ = get_user(con, guild_id, user_id)
            before = points

            if points < ROLL_COST:
                await interaction.response.send_message(
                    f"แต้มไม่พอจ้า 😭\nตอนนี้มี: **{points}** แต้ม",
                    ephemeral=True
                )
                return

            points -= ROLL_COST
            set_user_points(con, guild_id, user_id, points)
            after = points

        reward = roll_reward()

        # ผู้เล่นเห็นแบบ ephemeral
        await interaction.response.send_message(
            f"🎲 ผลกาชา: **{reward}**\nแต้ม: **{before} → {after}**",
            ephemeral=True
        )

        # Log แอดมิน
        await send_log(
            interaction.guild,
            f"🎲 GACHA | {interaction.user.mention} | ก่อน: {before} | หลัง: {after} | ได้: {reward}"
        )

    @discord.ui.button(label="📊 เช็คคะแนน", style=discord.ButtonStyle.secondary, custom_id="aura:checkpoints")
    async def checkpoints_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild_id:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        with sqlite3.connect(DB_PATH) as con:
            points, _ = get_user(con, guild_id, user_id)

        await interaction.response.send_message(
            f"คะแนนของคุณตอนนี้: **{points}** แต้ม ✅",
            ephemeral=True
        )


# =========================
# COMMANDS - SETUP PANELS
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx: commands.Context):
    ch_id = get_setting(ctx.guild.id, "daily_channel_id")
    if ch_id and ctx.channel.id != int(ch_id):
        return await ctx.send(f"ห้อง Daily ถูกตั้งไว้ที่ <#{ch_id}> แล้ว")

    embed = discord.Embed(
        title="✅ AURA DAILY POINT",
        description=f"กดรับได้วันละ 1 ครั้ง ได้ +{DAILY_AMOUNT} แต้ม",
        color=0x2ecc71
    )
    if DAILY_IMAGE_URL:
        embed.set_image(url=DAILY_IMAGE_URL)

    await ctx.send(embed=embed, view=DailyView())


@bot.command()
@commands.has_permissions(administrator=True)
async def setupgacha(ctx: commands.Context):
    ch_id = get_setting(ctx.guild.id, "gacha_channel_id")
    if ch_id and ctx.channel.id != int(ch_id):
        return await ctx.send(f"ห้อง Gacha ถูกตั้งไว้ที่ <#{ch_id}> แล้ว")

    embed = discord.Embed(
        title="🎲 AURA GACHA",
        description=f"กดสุ่มรางวัล ใช้ {ROLL_COST} แต้ม/ครั้ง",
        color=0x9b59b6
    )
    if GACHA_IMAGE_URL:
        embed.set_image(url=GACHA_IMAGE_URL)

    await ctx.send(embed=embed, view=GachaView())


@bot.command()
@commands.has_permissions(administrator=True)
async def setdailychannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "daily_channel_id", str(channel.id))
    await ctx.send(f"✅ ตั้งห้อง Daily Panel เป็น {channel.mention} แล้ว")


@bot.command()
@commands.has_permissions(administrator=True)
async def setgachachannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "gacha_channel_id", str(channel.id))
    await ctx.send(f"✅ ตั้งห้อง Gacha Panel เป็น {channel.mention} แล้ว")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "log_channel_id", str(channel.id))
    await ctx.send(f"✅ ตั้งห้อง Logs เป็น {channel.mention} แล้ว")


# =========================
# COMMANDS - VOICE CHANNELS (หลายห้อง)
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def addvoicechannel(ctx: commands.Context, channel: discord.VoiceChannel):
    add_voice_channel(ctx.guild.id, channel.id)
    await ctx.send(f"✅ เพิ่มห้องเสียงสำหรับสะสมแต้ม: {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def removevoicechannel(ctx: commands.Context, channel: discord.VoiceChannel):
    remove_voice_channel(ctx.guild.id, channel.id)
    await ctx.send(f"✅ ลบห้องเสียงออกจากรายการ: {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def listvoicechannels(ctx: commands.Context):
    ids = list_voice_channels(ctx.guild.id)
    if not ids:
        return await ctx.send("ยังไม่ได้ตั้งห้องเสียงเลย ใช้ `!addvoicechannel <ห้อง>`")

    lines = []
    for cid in ids:
        ch = ctx.guild.get_channel(cid)
        lines.append(ch.mention if ch else f"`{cid}`")
    await ctx.send("🎧 ห้องเสียงที่สะสมแต้มได้:\n" + "\n".join(lines))


# =========================
# VOICE REWARD LOOP
# =========================
@tasks.loop(minutes=VOICE_CHECK_EVERY_MIN)
async def voice_reward_loop():
    for guild in bot.guilds:
        voice_ids = list_voice_channels(guild.id)
        if not voice_ids:
            continue

        for vc_id in voice_ids:
            vc = guild.get_channel(vc_id)
            if not isinstance(vc, discord.VoiceChannel):
                continue

            for member in vc.members:
                if member.bot:
                    continue

                # mute condition: self_mute OR self_deaf OR server mute/deaf
                is_muted = bool(
                    member.voice and (
                        member.voice.self_mute or
                        member.voice.self_deaf or
                        member.voice.mute or
                        member.voice.deaf
                    )
                )

                with sqlite3.connect(DB_PATH) as con:
                    cur = con.cursor()
                    cur.execute("""
                        SELECT active_minutes, muted_streak_minutes
                        FROM voice_progress
                        WHERE guild_id=? AND user_id=? AND channel_id=?
                    """, (guild.id, member.id, vc_id))
                    row = cur.fetchone()

                    if not row:
                        active_minutes, muted_streak = 0, 0
                        cur.execute("""
                            INSERT INTO voice_progress (guild_id, user_id, channel_id, active_minutes, muted_streak_minutes)
                            VALUES (?, ?, ?, 0, 0)
                        """, (guild.id, member.id, vc_id))
                        con.commit()
                    else:
                        active_minutes, muted_streak = row

                    if is_muted:
                        muted_streak += VOICE_CHECK_EVERY_MIN
                        # ถ้า mute เกิน limit => รีเซ็ตชั่วโมงนั้น
                        if muted_streak >= VOICE_MUTE_LIMIT_MIN:
                            active_minutes = 0
                            muted_streak = 0
                            con.execute("""
                                UPDATE voice_progress
                                SET active_minutes=?, muted_streak_minutes=?
                                WHERE guild_id=? AND user_id=? AND channel_id=?
                            """, (active_minutes, muted_streak, guild.id, member.id, vc_id))
                            con.commit()

                            # DM แจ้ง
                            try:
                                await member.send(
                                    f"⛔ คุณ mute/deaf เกิน {VOICE_MUTE_LIMIT_MIN} นาทีในห้อง {vc.name}\n"
                                    f"ระบบรีเซ็ตเวลาสะสมของชั่วโมงนี้เป็น 0 แล้ว"
                                )
                            except:
                                pass
                        else:
                            con.execute("""
                                UPDATE voice_progress
                                SET muted_streak_minutes=?
                                WHERE guild_id=? AND user_id=? AND channel_id=?
                            """, (muted_streak, guild.id, member.id, vc_id))
                            con.commit()

                        continue  # muted ไม่สะสม

                    # ไม่ muted => สะสมเวลา
                    muted_streak = 0
                    active_minutes += VOICE_CHECK_EVERY_MIN

                    # ครบเวลา -> ให้แต้ม (สะสมต่อได้เรื่อยๆ)
                    if active_minutes >= VOICE_REWARD_MINUTES:
                        # หักเป็นรอบๆ
                        active_minutes = active_minutes - VOICE_REWARD_MINUTES

                        # เพิ่มแต้ม user
                        pts, _ = get_user(con, guild.id, member.id)
                        before = pts
                        pts += VOICE_REWARD_POINTS
                        set_user_points(con, guild.id, member.id, pts)

                        # DM แจ้ง
                        try:
                            await member.send(
                                f"🎧 อยู่ห้องเสียง {vc.name} ครบ {VOICE_REWARD_MINUTES} นาทีแล้ว!\n"
                                f"ได้รับ +{VOICE_REWARD_POINTS} แต้ม ✅\n"
                                f"แต้ม: {before} → {pts}"
                            )
                        except:
                            pass

                        # Log
                        await send_log(
                            guild,
                            f"🎧 VOICE | {member.mention} | +{VOICE_REWARD_POINTS} | {before} → {pts} | ห้อง: {vc.name}"
                        )

                    con.execute("""
                        UPDATE voice_progress
                        SET active_minutes=?, muted_streak_minutes=?
                        WHERE guild_id=? AND user_id=? AND channel_id=?
                    """, (active_minutes, muted_streak, guild.id, member.id, vc_id))
                    con.commit()


@voice_reward_loop.before_loop
async def before_voice_loop():
    await bot.wait_until_ready()


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    init_db()
    try:
        bot.add_view(DailyView())
        bot.add_view(GachaView())
    except:
        pass

    if not voice_reward_loop.is_running():
        voice_reward_loop.start()

    print(f"✅ Logged in as {bot.user}")


# =========================
# RUN
# =========================
bot.run(TOKEN)
