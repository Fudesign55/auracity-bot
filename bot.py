import os
import sqlite3
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from myserver import server_on
server_on()
# ======================
# โหลด .env
# ======================
load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ ไม่พบ DISCORD_TOKEN ในไฟล์ .env (ต้องชื่อ .env และอยู่โฟลเดอร์เดียวกับ bot.py)")

# ======================
# CONFIG
# ======================
TH_TZ = ZoneInfo("Asia/Bangkok")

DAILY_AMOUNT = 10
ROLL_COST = 10

VOICE_REWARD_MINUTES = 1   # ครบ 60 นาที
VOICE_REWARD_POINTS = 10    # ได้ 10 แต้ม
VOICE_CHECK_EVERY_MIN = 1   # เช็คทุก 1 นาที
VOICE_MUTE_LIMIT_MIN = 30   # ปิดไมค์/ปิดเสียงต่อเนื่องเกิน 30 นาที => หยุดนับจนกว่าจะกลับมา

REWARDS = [
    ("เสียใจด้วยไม่ได้รางวัล 😭", 60),
    ("เงินเขียว 5,000 🟩", 25),
    ("เงินเขียว 10,000 🟩", 10),
    ("เงินแดง 3,000 🟥", 4),
    ("สกินไม้สุดแรร์ 🌟", 1),
]

DB_PATH = "points.db"

# ======================
# DISCORD INTENTS (สำคัญกับ voice)
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True         # สำคัญ: ให้เห็นสมาชิก/ห้องเสียง
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ======================
# DB
# ======================
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            points INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT
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
        # เก็บเวลาสะสม voice + สถานะ mute ต่อเนื่อง
        con.execute("""
        CREATE TABLE IF NOT EXISTS voice_progress (
            user_id INTEGER NOT NULL,
            voice_channel_id TEXT NOT NULL,
            active_minutes INTEGER NOT NULL DEFAULT 0,
            muted_streak_minutes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, voice_channel_id)
        )
        """)
        con.commit()

def today_str_th():
    return datetime.now(TH_TZ).strftime("%Y-%m-%d")

def roll_reward():
    total = sum(w for _, w in REWARDS)
    r = random.uniform(0, total)
    upto = 0
    for reward, weight in REWARDS:
        upto += weight
        if upto >= r:
            return reward
    return REWARDS[-1][0]

def get_user(con, user_id: int):
    cur = con.cursor()
    cur.execute("SELECT points, last_daily FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_id, points, last_daily) VALUES (?, 0, NULL)", (user_id,))
        con.commit()
        return 0, None
    return row[0], row[1]

def set_points(con, user_id: int, points: int):
    con.execute("UPDATE users SET points=? WHERE user_id=?", (points, user_id))
    con.commit()

def set_last_daily(con, user_id: int, date_str: str):
    con.execute("UPDATE users SET last_daily=? WHERE user_id=?", (date_str, user_id))
    con.commit()

def set_setting(guild_id, key, value):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO settings (guild_id, key, value) VALUES (?, ?, ?)",
            (guild_id, key, value)
        )
        con.commit()

def get_setting(guild_id, key):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = cur.fetchone()
        return row[0] if row else None



def vp_get(con, user_id: int, voice_channel_id: int):
    cur = con.cursor()
    cur.execute("""
        SELECT active_minutes, muted_streak_minutes
        FROM voice_progress
        WHERE user_id=? AND voice_channel_id=?
    """, (user_id, str(voice_channel_id)))
    row = cur.fetchone()
    if row is None:
        cur.execute("""
            INSERT INTO voice_progress (user_id, voice_channel_id, active_minutes, muted_streak_minutes)
            VALUES (?, ?, 0, 0)
        """, (user_id, str(voice_channel_id)))
        con.commit()
        return 0, 0
    return row[0], row[1]

def vp_set(con, user_id: int, voice_channel_id: int, active_minutes: int, muted_streak_minutes: int):
    con.execute("""
        UPDATE voice_progress
        SET active_minutes=?, muted_streak_minutes=?
        WHERE user_id=? AND voice_channel_id=?
    """, (active_minutes, muted_streak_minutes, user_id, str(voice_channel_id)))
    con.commit()

# ======================
# VIEWS (แยกห้อง daily / roll)
# ======================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=f"✅ รับ Daily +{DAILY_AMOUNT}", style=discord.ButtonStyle.success, custom_id="aura:daily")
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        daily_ch = get_setting(interaction.guild_id, "daily_channel_id")
        if daily_ch and str(interaction.channel_id) != str(daily_ch):
            return await interaction.response.send_message(
                "ปุ่ม Daily ใช้ได้เฉพาะห้อง Daily ที่ตั้งไว้เท่านั้นนะ 💜",
                ephemeral=True
            )

        user_id = interaction.user.id
        with sqlite3.connect(DB_PATH) as con:
            points, last_daily = get_user(con, user_id)
            today = today_str_th()

            if last_daily == today:
                return await interaction.response.send_message(
                    f"วันนี้รับไปแล้วน้า 😝\nแต้มตอนนี้: **{points}**",
                    ephemeral=True
                )

            points += DAILY_AMOUNT
            set_points(con, user_id, points)
            set_last_daily(con, user_id, today)

        await interaction.response.send_message(
            f"รับ Daily สำเร็จ! ได้ **+{DAILY_AMOUNT}** แต้ม ✅\nแต้มตอนนี้: **{points}**",
            ephemeral=True
        )

class RollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label=f"🎲 สุ่มรางวัล (เสีย {ROLL_COST} แต้ม)",
        style=discord.ButtonStyle.primary,
        custom_id="aura:roll"
    )
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        roll_ch = get_setting(interaction.guild_id, "roll_channel_id")
        if roll_ch and str(interaction.channel_id) != str(roll_ch):
            return await interaction.response.send_message(
                "ปุ่มสุ่มใช้ได้เฉพาะห้องสุ่มรางวัลที่ตั้งไว้เท่านั้นน้า 💜",
                ephemeral=True
            )

        user_id = interaction.user.id
        with sqlite3.connect(DB_PATH) as con:
            points, _ = get_user(con, user_id)

            if points < ROLL_COST:
                return await interaction.response.send_message(
                    f"แต้มไม่พอจ้า ต้องใช้ **{ROLL_COST}** แต้ม\nแต้มตอนนี้: **{points}**",
                    ephemeral=True
                )

            points -= ROLL_COST
            reward = roll_reward()
            set_points(con, user_id, points)

        await interaction.response.send_message(
            f"🎉 ผลสุ่มของ {interaction.user.mention}\n"
            f"รางวัล: **{reward}**\n"
            f"แต้มคงเหลือ: **{points}**"
        )

    @discord.ui.button(
        label="📊 เช็คคะแนน",
        style=discord.ButtonStyle.secondary,
        custom_id="aura:checkpoints"
    )
    async def checkpoints_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        with sqlite3.connect(DB_PATH) as con:
            points, _ = get_user(con, user_id)

        await interaction.response.send_message(
            f"คะแนนของคุณตอนนี้: **{points}** แต้ม 🪙",
            ephemeral=True
        )


# ======================
# VOICE REWARD LOOP
# ======================
def is_muted_or_deaf(member: discord.Member) -> bool:
    vs = member.voice
    if not vs:
        return True
    # self_mute/self_deaf = ผู้ใช้กดเอง
    # mute/deaf = ถูกเซิร์ฟเวอร์ mute/deaf
    return bool(vs.self_mute or vs.self_deaf or vs.mute or vs.deaf)

@tasks.loop(minutes=VOICE_CHECK_EVERY_MIN)
async def voice_reward_loop():
    for guild in bot.guilds:
        voice_channel_id = get_setting(guild.id, "voice_channel_id")
        if not voice_channel_id:
            continue

        vc_id = int(voice_channel_id)
        channel = guild.get_channel(vc_id)
        if channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            continue

        for member in list(channel.members):
            if member.bot:
                continue

            # ... (โค้ดนับเวลาเดิมของฟุอยู่ต่อจากนี้ได้เลย)


            muted = is_muted_or_deaf(member)

            with sqlite3.connect(DB_PATH) as con:
                active_min, muted_streak = vp_get(con, member.id, vc_id)

                if muted:
                    muted_streak += VOICE_CHECK_EVERY_MIN

                    # ❗ ถ้า mute เกิน 30 นาที → รีเซ็ตเวลาสะสมในชั่วโมงนี้
                    if muted_streak >= VOICE_MUTE_LIMIT_MIN:
                        active_min = 0
                        muted_streak = 0

                    vp_set(con, member.id, vc_id, active_min, muted_streak)
                    continue

                # ไม่ muted แล้ว => reset streak
                muted_streak = 0

                # เพิ่มเวลาที่ “นับได้”
                active_min += VOICE_CHECK_EVERY_MIN

                # ถ้าครบ 60 นาที ให้รางวัล (สะสมต่อได้)
                if active_min >= VOICE_REWARD_MINUTES:
                    # แจกเป็นจำนวนครั้งตามชั่วโมงที่ครบ
                    times = active_min // VOICE_REWARD_MINUTES
                    gain = times * VOICE_REWARD_POINTS
                    leftover = active_min % VOICE_REWARD_MINUTES

                    # เพิ่มแต้ม
                    points, _ = get_user(con, member.id)
                    points += gain
                    set_points(con, member.id, points)

                    # อัปเดตเหลือเวลาสะสมที่ยังไม่ครบชั่วโมง
                    vp_set(con, member.id, vc_id, leftover, muted_streak)

                    # แจ้ง DM (ไม่รกห้อง)
                    try:
                        await member.send(
                            f"🎧 อยู่ห้องเสียงครบ {times} ชม. ได้ **+{gain}** แต้ม!\n"
                            f"แต้มตอนนี้: **{points}**"
                        )
                    except:
                        pass
                else:
                    vp_set(con, member.id, vc_id, active_min, muted_streak)

@voice_reward_loop.before_loop
async def before_voice_loop():
    await bot.wait_until_ready()

# ======================
# EVENTS
# ======================
@bot.event
async def on_ready():
    init_db()

    # ทำให้ปุ่ม persistent หลังรีสตาร์ท
    bot.add_view(DailyView())
    bot.add_view(RollView())

    if not voice_reward_loop.is_running():
        voice_reward_loop.start()

    print(f"🤖 Logged in as {bot.user}")

# ======================
# COMMANDS (User)
# ======================
@bot.command()
async def points(ctx):
    with sqlite3.connect(DB_PATH) as con:
        pts, last = get_user(con, ctx.author.id)
    last_txt = last if last else "ยังไม่เคยรับ"
    await ctx.send(f"แต้มของ {ctx.author.mention} = **{pts}** | Daily ล่าสุด: **{last_txt}**")

# ======================
# COMMANDS (Admin setup)
# ======================
@bot.command()
@commands.has_permissions(administrator=True)
async def setdailychannel(ctx):
    set_setting(ctx.guild.id, "daily_channel_id", str(ctx.channel.id))
    await ctx.send(f"✅ ตั้งห้อง Daily แล้ว: {ctx.channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setrollchannel(ctx):
    set_setting(ctx.guild.id, "roll_channel_id", str(ctx.channel.id))
    await ctx.send(f"✅ ตั้งห้องสุ่มรางวัลแล้ว: {ctx.channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicechannel(ctx, voice_channel_id: int):
    set_setting(ctx.guild.id, "voice_channel_id", str(voice_channel_id))
    await ctx.send(f"✅ ตั้งห้องเสียงสะสมแต้มแล้ว: `{voice_channel_id}`")

@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx):
    embed = discord.Embed(
        title="✅ AURA DAILY POINT",
        description=f"กดรับได้วันละ 1 ครั้ง ได้ **+{DAILY_AMOUNT}** แต้ม"
    )

    embed.set_image(url="https://media.discordapp.net/attachments/1241811407310164030/1462800299029696637/1.png")

    await ctx.send(embed=embed, view=DailyView())

@bot.command()
@commands.has_permissions(administrator=True)
async def setuproll(ctx):
    embed = discord.Embed(
        title="🎲 AURA GACHA",
        description=f"กดสุ่มรางวัล ใช้ **{ROLL_COST}** แต้ม/ครั้ง"
    )

    embed.set_image(url="https://media.discordapp.net/attachments/1241811407310164030/1462803920156889214/unnamed.jpg")

    await ctx.send(embed=embed, view=RollView())

@bot.command()
@commands.has_permissions(administrator=True)
async def showsettings(ctx):
    d = get_setting(ctx.guild.id, "daily_channel_id")
    r = get_setting(ctx.guild.id, "roll_channel_id")
    v = get_setting(ctx.guild.id, "voice_channel_id")
    await ctx.send(
        "⚙️ Settings\n"
        f"- daily_channel_id: `{d}`\n"
        f"- roll_channel_id: `{r}`\n"
        f"- voice_channel_id: `{v}`"
    )

@setdailychannel.error
@setrollchannel.error
@setvoicechannel.error
@setupdaily.error
@setuproll.error
@showsettings.error
async def admin_cmd_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("ต้องเป็นแอดมินถึงใช้คำสั่งนี้ได้ ❌")

# ======================
# RUN
# ======================
bot.run(TOKEN)
