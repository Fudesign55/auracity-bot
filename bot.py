import os
import random
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ==============
# Optional keep-alive (Railway/health)
# ==============
try:
    from myserver import server_on
except Exception:
    server_on = None


# ======================
# CONFIG (แก้ได้ในโค้ดถ้าต้องการ)
# ======================
TH_TZ = ZoneInfo("Asia/Bangkok")
DB_PATH = "points.db"

DEFAULT_DAILY_AMOUNT = 10
DEFAULT_ROLL_COST = 10

DEFAULT_VOICE_REWARD_MINUTES = 60
DEFAULT_VOICE_REWARD_POINTS = 10
DEFAULT_VOICE_CHECK_EVERY_MIN = 1
DEFAULT_VOICE_MUTE_LIMIT_MIN = 30

# กาชา (แก้รางวัล/เรทตรงนี้ได้เลย)
# rate = น้ำหนัก (ยิ่งมากยิ่งออกบ่อย)
GACHA_REWARDS = [
    {"name": "เงินเขียว 5,000", "rate": 60},
    {"name": "เงินเขียว 10,000", "rate": 30},
    {"name": "ไอเท็มสุ่ม (ทั่วไป)", "rate": 9},
    {"name": "ไอเท็มหายาก (Rare)", "rate": 1},
]


# ======================
# Discord bot setup
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ======================
# DB helpers
# ======================
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (guild_id, key)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_progress (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                active_minutes INTEGER NOT NULL DEFAULT 0,
                muted_streak_minutes INTEGER NOT NULL DEFAULT 0,
                last_tick_utc TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        con.commit()


def set_setting(guild_id: int, key: str, value: str):
    with db_connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings (guild_id, key, value) VALUES (?, ?, ?)",
            (guild_id, key, str(value)),
        )
        con.commit()


def get_setting(guild_id: int, key: str, default=None):
    with db_connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?",
            (guild_id, key),
        ).fetchone()
        return row["value"] if row else default


def ensure_user(guild_id: int, user_id: int):
    with db_connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id, points, last_daily) VALUES (?, ?, 0, NULL)",
            (guild_id, user_id),
        )
        con.commit()


def get_points(guild_id: int, user_id: int) -> int:
    ensure_user(guild_id, user_id)
    with db_connect() as con:
        row = con.execute(
            "SELECT points FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        return int(row["points"]) if row else 0


def set_points(guild_id: int, user_id: int, points: int):
    ensure_user(guild_id, user_id)
    with db_connect() as con:
        con.execute(
            "UPDATE users SET points=? WHERE guild_id=? AND user_id=?",
            (int(points), guild_id, user_id),
        )
        con.commit()


def add_points(guild_id: int, user_id: int, amount: int):
    before = get_points(guild_id, user_id)
    after = before + int(amount)
    set_points(guild_id, user_id, after)
    return before, after


def can_claim_daily(guild_id: int, user_id: int) -> bool:
    ensure_user(guild_id, user_id)
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    with db_connect() as con:
        row = con.execute(
            "SELECT last_daily FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        last_daily = row["last_daily"] if row else None
        return last_daily != today


def set_daily_claimed(guild_id: int, user_id: int):
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    with db_connect() as con:
        con.execute(
            "UPDATE users SET last_daily=? WHERE guild_id=? AND user_id=?",
            (today, guild_id, user_id),
        )
        con.commit()


def add_voice_channel(guild_id: int, channel_id: int):
    with db_connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO voice_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        con.commit()


def remove_voice_channel(guild_id: int, channel_id: int):
    with db_connect() as con:
        con.execute(
            "DELETE FROM voice_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )
        con.commit()


def list_voice_channels(guild_id: int):
    with db_connect() as con:
        rows = con.execute(
            "SELECT channel_id FROM voice_channels WHERE guild_id=? ORDER BY channel_id",
            (guild_id,),
        ).fetchall()
        return [int(r["channel_id"]) for r in rows]


def get_or_create_voice_progress(guild_id: int, user_id: int):
    with db_connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO voice_progress
            (guild_id, user_id, channel_id, active_minutes, muted_streak_minutes, last_tick_utc)
            VALUES (?, ?, NULL, 0, 0, NULL)
            """,
            (guild_id, user_id),
        )
        con.commit()

        row = con.execute(
            "SELECT * FROM voice_progress WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        return row


def update_voice_progress(guild_id: int, user_id: int, channel_id, active_minutes: int, muted_streak: int):
    with db_connect() as con:
        con.execute(
            """
            UPDATE voice_progress
            SET channel_id=?, active_minutes=?, muted_streak_minutes=?, last_tick_utc=?
            WHERE guild_id=? AND user_id=?
            """,
            (
                channel_id,
                int(active_minutes),
                int(muted_streak),
                datetime.now(timezone.utc).isoformat(),
                guild_id,
                user_id,
            ),
        )
        con.commit()


# ======================
# Logging (แยก 2 ห้อง)
# ======================
async def _send_log_to_channel_id(guild: discord.Guild, channel_id, text: str):
    if not guild or not channel_id:
        return
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return
    try:
        await ch.send(text)
    except Exception:
        pass


async def send_daily_log(guild: discord.Guild, text: str):
    ch_id = get_setting(guild.id, "daily_log_channel_id", None)
    await _send_log_to_channel_id(guild, ch_id, text)


async def send_gacha_log(guild: discord.Guild, text: str):
    ch_id = get_setting(guild.id, "gacha_log_channel_id", None)
    await _send_log_to_channel_id(guild, ch_id, text)


# ======================
# Gacha helper
# ======================
def roll_reward_name() -> str:
    total = sum(r["rate"] for r in GACHA_REWARDS)
    pick = random.uniform(0, total)
    cur = 0.0
    for r in GACHA_REWARDS:

        cur += r["rate"]
        if pick <= cur:
            return r["name"]
    return GACHA_REWARDS[-1]["name"]


# ======================
# UI Views (ไม่มีปุ่มเช็คคะแนนแล้ว)
# ======================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ กดรับ Daily",
        style=discord.ButtonStyle.success,
        custom_id="aura:daily"
    )
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        gid = interaction.guild.id
        uid = interaction.user.id
        daily_amount = int(get_setting(gid, "daily_amount", DEFAULT_DAILY_AMOUNT))

        pts_before = get_points(gid, uid)

        if not can_claim_daily(gid, uid):
            await interaction.response.send_message(
                f"วันนี้รับไปแล้วน้า 😆\nคะแนนตอนนี้: **{pts_before}** แต้ม",
                ephemeral=True
            )
            return

        before, after = add_points(gid, uid, daily_amount)
        set_daily_claimed(gid, uid)

        await interaction.response.send_message(
            f"รับ Daily แล้ว ✅ +{daily_amount} แต้ม\nคะแนน: **{before} → {after}**",
            ephemeral=True
        )

        # LOG (Daily แยกห้อง)
        await send_daily_log(
            interaction.guild,
            "\n".join([
                "🟩 **DAILY CLAIM**",
                f"👤 ผู้เล่น: <@{uid}>",
                f"➕ ได้รับ: +{daily_amount} แต้ม",
                f"📊 คะแนน: {before} → {after}",
            ])
        )


class RollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎲 สุ่มรางวัล",
        style=discord.ButtonStyle.danger,
        custom_id="aura:roll"
    )
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        gid = interaction.guild.id
        uid = interaction.user.id

        # บังคับห้อง (ถ้ามีตั้ง)
        roll_ch = get_setting(gid, "roll_channel_id", None)
        if roll_ch and int(roll_ch) != interaction.channel_id:
            return await interaction.response.send_message(
                "ปุ่มสุ่มรางวัลใช้ได้เฉพาะห้องที่ตั้งไว้นะ 💜",
                ephemeral=True
            )

        roll_cost = int(get_setting(gid, "roll_cost", DEFAULT_ROLL_COST))
        pts_before = get_points(gid, uid)

        if pts_before < roll_cost:
            return await interaction.response.send_message(
                f"แต้มไม่พอจ้า 😅 ต้องใช้ {roll_cost} แต้ม/ครั้ง\nตอนนี้คุณมี: **{pts_before}** แต้ม",
                ephemeral=True
            )

        # ✅ หักแต้ม + สุ่ม (อยู่ในปุ่มสุ่มเท่านั้น)
        set_points(gid, uid, pts_before - roll_cost)
        reward = roll_reward_name()
        pts_after = get_points(gid, uid)

        await interaction.response.send_message(
            f"🎉 คุณได้รางวัล: **{reward}**\n"
            f"💰แต้มคงเหลือ: **{pts_after}**\n\n"
            "📸 **หลังจากคุณกดสุ่มแล้วให้แคปรูปเพื่อเป็นหลักฐานในการยืนยันเพื่อรับของรางวัลของ**",
            ephemeral=True
        )

        # LOG (Gacha แยกห้อง)
        await send_gacha_log(
            interaction.guild,
            "\n".join([
                "🎲 **AURA GACHA**",
                f"👤 ผู้เล่น: <@{uid}>",
                f"🎁 รางวัล: **{reward}**",
                f"💸 ใช้แต้ม: -{roll_cost}",
                f"📊 คะแนน: {pts_before} → {pts_after}",
            ])
        )

    @discord.ui.button(
        label="📊 เช็คคะแนน",
        style=discord.ButtonStyle.secondary,
        custom_id="aura:checkpoints"
    )
    async def checkpoints_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ", ephemeral=True)

        gid = interaction.guild.id
        uid = interaction.user.id
        pts = get_points(gid, uid)

        await interaction.response.send_message(
            f"คะแนนของคุณตอนนี้: **{pts}** แต้ม ✅",
            ephemeral=True
        )


        # หักแต้ม + สุ่ม
        set_points(gid, uid, pts_before - roll_cost)
        reward = roll_reward_name()
        pts_after = get_points(gid, uid)

        # ผลลัพธ์ให้คนกด (Ephemeral)
        await interaction.response.send_message(
            "\n".join([
                f"🎉 คุณได้รางวัล: **{reward}**",
                f"แต้มคงเหลือ: **{pts_after}**",
                "",
            ]),
            ephemeral=True
        )

        # LOG (Gacha แยกห้อง)
        await send_gacha_log(
            interaction.guild,
            "\n".join([
                "🎲 **AURA GACHA**",
                f"👤 ผู้เล่น: <@{uid}>",
                f"🎁 รางวัล: **{reward}**",
                f"💸 ใช้แต้ม: -{roll_cost}",
                f"📊 คะแนน: {pts_before} → {pts_after}",
            ])
        )


# ======================
# Embeds (setup commands)
# ======================
def build_gacha_embed(guild_id: int) -> discord.Embed:
    roll_cost = int(get_setting(guild_id, "roll_cost", DEFAULT_ROLL_COST))
    embed = discord.Embed(
        title="AURA GACHA",
        description=(
            f"กดสุ่มรางวัล ใช้ **{roll_cost}** แต้ม/ครั้ง\n\n"
            "**กดสุ่มแล้วให้แคปรูปเพื่อเป็นหลักฐานในการรับของรางวัลของคุณ**"
        ),
        color=0xFF0033
    )

    img = get_setting(guild_id, "gacha_image_url", None)
    if img:
        embed.set_image(url=img)

    return embed


def build_daily_embed(guild_id: int) -> discord.Embed:
    daily_amount = int(get_setting(guild_id, "daily_amount", DEFAULT_DAILY_AMOUNT))
    embed = discord.Embed(
        title="DAILY CLAIM",
        description=f"สามารถกดรับ Daily ได้ 1 ครั้งต่อวันเท่านั้น (**+{daily_amount}** แต้ม)",
        color=0x5865F2
    )

    img = get_setting(guild_id, "daily_image_url", None)
    if img:
        embed.set_image(url=img)

    return embed



# ======================
# Commands
# ======================
@bot.command()
async def points(ctx: commands.Context):
    if not ctx.guild:
        return await ctx.send("ใช้ในเซิร์ฟเวอร์เท่านั้นนะ")
    pts = get_points(ctx.guild.id, ctx.author.id)
    await ctx.send(f"<@{ctx.author.id}> ตอนนี้มี **{pts}** แต้ม ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setupgacha(ctx: commands.Context):
    if not ctx.guild:
        return
    embed = build_gacha_embed(ctx.guild.id)
    await ctx.send(embed=embed, view=RollView())


@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx: commands.Context):
    if not ctx.guild:
        return
    embed = build_daily_embed(ctx.guild.id)
    await ctx.send(embed=embed, view=DailyView())


# ---- LOG CHANNELS (แยกกัน)
@bot.command()
@commands.has_permissions(administrator=True)
async def setdailylogchannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "daily_log_channel_id", str(channel.id))
    await ctx.send(f"ตั้งห้อง Daily Logs เป็น {channel.mention} แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setgachalogchannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "gacha_log_channel_id", str(channel.id))
    await ctx.send(f"ตั้งห้อง Gacha Logs เป็น {channel.mention} แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setrollchannel(ctx: commands.Context, channel: discord.TextChannel):
    set_setting(ctx.guild.id, "roll_channel_id", str(channel.id))
    await ctx.send(f"ตั้งห้องกาชาให้กดได้เฉพาะ {channel.mention} แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setdailyamount(ctx: commands.Context, amount: int):
    set_setting(ctx.guild.id, "daily_amount", str(int(amount)))
    await ctx.send(f"ตั้ง Daily เป็น {amount} แต้ม/วัน แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setrollcost(ctx: commands.Context, amount: int):
    set_setting(ctx.guild.id, "roll_cost", str(int(amount)))
    await ctx.send(f"ตั้งค่ากาชาใช้ {amount} แต้ม/ครั้ง แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setgachaimage(ctx: commands.Context, url: str):
    set_setting(ctx.guild.id, "gacha_image_url", url)
    await ctx.send("ตั้งรูปกาชาแล้ว ✅ (สั่ง !setupgacha ใหม่เพื่อดูผล)")


@bot.command()
@commands.has_permissions(administrator=True)
async def setdailyimage(ctx: commands.Context, url: str):
    set_setting(ctx.guild.id, "daily_image_url", url)
    await ctx.send("ตั้งรูป Daily แล้ว ✅ (สั่ง !setupdaily ใหม่เพื่อดูผล)")


@bot.command()
@commands.has_permissions(administrator=True)
async def addvoicechannel(ctx: commands.Context, channel: discord.VoiceChannel):
    add_voice_channel(ctx.guild.id, channel.id)
    await ctx.send(f"เพิ่มห้องเสียง {channel.mention} แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def removevoicechannel(ctx: commands.Context, channel: discord.VoiceChannel):
    remove_voice_channel(ctx.guild.id, channel.id)
    await ctx.send(f"ลบห้องเสียง {channel.mention} แล้ว ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def listvoicechannels(ctx: commands.Context):
    ids = list_voice_channels(ctx.guild.id)
    if not ids:
        return await ctx.send("ยังไม่ได้ตั้งห้องเสียงเลยน้า (ใช้ !addvoicechannel)")
    names = []
    for cid in ids:
        ch = ctx.guild.get_channel(cid)
        names.append(ch.mention if ch else f"`{cid}`")
    await ctx.send("ห้องเสียงที่นับเวลาได้:\n" + "\n".join(names))


@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicerewardminutes(ctx: commands.Context, minutes: int):
    set_setting(ctx.guild.id, "voice_reward_minutes", str(int(minutes)))
    await ctx.send(f"ตั้ง voice reward = ครบ {minutes} นาที ได้แต้ม ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicerewardpoints(ctx: commands.Context, points: int):
    set_setting(ctx.guild.id, "voice_reward_points", str(int(points)))
    await ctx.send(f"ตั้ง voice reward = ได้ {points} แต้ม ต่อรอบ ✅")


@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicecheck(ctx: commands.Context, minutes: int):
    set_setting(ctx.guild.id, "voice_check_every_min", str(int(minutes)))
    await ctx.send("ตอนนี้ loop เสียงล็อคไว้ที่ 1 นาทีเพื่อความนิ่ง ✅ (ยังไม่ได้ใช้ค่านี้จริง)")


@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicemutelimit(ctx: commands.Context, minutes: int):
    set_setting(ctx.guild.id, "voice_mute_limit_min", str(int(minutes)))
    await ctx.send(f"ตั้ง mute limit = เกิน {minutes} นาที รีเซ็ตเวลาสะสม ✅")


# ======================
# Voice tracking loop (ไม่มีส่ง LOGS ห้องแอดมิน)
# ======================
def is_member_effectively_muted(member: discord.Member) -> bool:
    vs = member.voice
    if not vs:
        return True
    return bool(vs.self_mute or vs.self_deaf or vs.mute or vs.deaf)


@tasks.loop(minutes=1)
async def voice_tick():
    for guild in bot.guilds:
        allowed = set(list_voice_channels(guild.id))
        if not allowed:
            continue

        reward_minutes = int(get_setting(guild.id, "voice_reward_minutes", DEFAULT_VOICE_REWARD_MINUTES))
        reward_points = int(get_setting(guild.id, "voice_reward_points", DEFAULT_VOICE_REWARD_POINTS))
        mute_limit = int(get_setting(guild.id, "voice_mute_limit_min", DEFAULT_VOICE_MUTE_LIMIT_MIN))

        for member in guild.members:
            if member.bot:
                continue

            vs = member.voice
            if not vs or not vs.channel:
                row = get_or_create_voice_progress(guild.id, member.id)
                if row["active_minutes"] != 0 or row["muted_streak_minutes"] != 0 or row["channel_id"] is not None:
                    update_voice_progress(guild.id, member.id, None, 0, 0)
                continue

            if vs.channel.id not in allowed:
                row = get_or_create_voice_progress(guild.id, member.id)
                if row["active_minutes"] != 0 or row["muted_streak_minutes"] != 0 or row["channel_id"] != vs.channel.id:
                    update_voice_progress(guild.id, member.id, vs.channel.id, 0, 0)
                continue

            row = get_or_create_voice_progress(guild.id, member.id)
            active = int(row["active_minutes"])
            muted_streak = int(row["muted_streak_minutes"])

            if is_member_effectively_muted(member):
                muted_streak += 1
                if muted_streak >= mute_limit:
                    active = 0
                update_voice_progress(guild.id, member.id, vs.channel.id, active, muted_streak)
                continue

            muted_streak = 0
            active += 1

            if active >= reward_minutes:
                before, after = add_points(guild.id, member.id, reward_points)
                active = active - reward_minutes

                # DM ผู้เล่น
                try:
                    await member.send(
                        f"🎧 คุณอยู่ห้องเสียงครบ {reward_minutes} นาทีแล้ว!\n"
                        f"ได้รับ +{reward_points} แต้ม ✅\n"
                        f"คะแนน: {before} → {after}"
                    )
                except Exception:
                    pass

            update_voice_progress(guild.id, member.id, vs.channel.id, active, muted_streak)


@voice_tick.before_loop
async def before_voice_tick():
    await bot.wait_until_ready()


# ======================
# Events
# ======================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    bot.add_view(RollView())
    bot.add_view(DailyView())

    if not voice_tick.is_running():
        voice_tick.start()


# ======================
# Main
# ======================
def main():
    load_dotenv()
    init_db()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ ไม่พบ DISCORD_TOKEN ใน .env หรือ Railway Variables")

    if server_on:
        try:
            server_on()
        except Exception:
            pass

    bot.run(token)


if __name__ == "__main__":
    main()
