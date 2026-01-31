import os
import random
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client, Client


# ==============
# Optional keep-alive (Railway/health)
# ==============
try:
    from myserver import server_on
except Exception:
    server_on = None


# ======================
# CONFIG & SUPABASE SETUP
# ======================
load_dotenv()
TH_TZ = ZoneInfo("Asia/Bangkok")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ❗ กันเคส ENV ไม่ครบแล้วรันมั่ว (ทำให้เอ๋อ/รีสตาร์ทวน)
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL / SUPABASE_KEY in environment")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ค่ามาตรฐานที่จะใช้ถ้าในฐานข้อมูลยังไม่ได้ตั้งค่า
DEFAULT_DAILY_AMOUNT = 10
DEFAULT_ROLL_COST = 10
DEFAULT_VOICE_REWARD_MINUTES = 60
DEFAULT_VOICE_REWARD_POINTS = 10
DEFAULT_VOICE_MUTE_LIMIT_MIN = 30

GACHA_REWARDS = [
    {"name": "สกินสุดแรร์ทอมแอนเจอรี่", "rate": 0.2},
    {"name": "สกินสุดแรร์ชีส", "rate": 0.2},
    {"name": "อาหารหมูกระทะ 6 ชม.", "rate": 3},
    {"name": "เงินเขียว 10,000.-", "rate": 3},
    {"name": "เงินเขียว 8,000.-", "rate": 5},
    {"name": "เงินเขี่ยว 4,000.-", "rate": 8.8},
    {"name": "เหรียญออนไลน์ 1 เหรียญ", "rate": 5},
    {"name": "เสียใจด้วยคุณไม่ได้รางวัลร้องไห้สะสิ", "rate": 74.8},
]


# ======================
# Discord bot setup
# ======================
intents = discord.Intents.default()
intents.message_content = True   # ต้องเปิดใน Discord Dev Portal ด้วย
intents.members = True           # ต้องเปิดใน Discord Dev Portal ด้วย (SERVER MEMBERS INTENT)
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ======================
# Robust DB executor (กันบอทล้ม)
# ======================
def _sb_err(label: str, e: Exception):
    print(f"[SB-ERROR] {label}: {type(e).__name__}: {e}")

async def sb_async(label: str, fn, retries: int = 3, base_delay: float = 0.6):
    """
    Run blocking Supabase calls in a thread with retries.
    Never crashes the whole process.
    """
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            _sb_err(label, e)
            # retry with backoff
            if attempt < retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                return None

def sb_sync(label: str, fn, retries: int = 2):
    """
    Sync version for very simple paths (still guarded).
    """
    last = None
    for _ in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            _sb_err(label, e)
    return None


# ======================
# DB helpers (Supabase)
# ======================
def set_setting(guild_id: int, key: str, value: str):
    # settings ควร unique (guild_id, key)
    return sb_sync(
        f"set_setting {key}",
        lambda: supabase.table("settings").upsert(
            {"guild_id": guild_id, "key": key, "value": str(value)},
            on_conflict="guild_id,key"
        ).execute()
    )

def get_setting(guild_id: int, key: str, default=None):
    res = sb_sync(
        f"get_setting {key}",
        lambda: supabase.table("settings").select("value").eq("guild_id", guild_id).eq("key", key).execute()
    )
    if res and getattr(res, "data", None):
        return res.data[0].get("value", default)
    return default

def get_points(guild_id: int, user_id: int) -> int:
    res = sb_sync(
        "get_points",
        lambda: supabase.table("users").select("points").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    )
    if not res or not getattr(res, "data", None):
        # create row
        sb_sync(
            "users insert",
            lambda: supabase.table("users").insert(
                {"guild_id": guild_id, "user_id": user_id, "points": 0}
            ).execute()
        )
        return 0
    return int(res.data[0].get("points") or 0)

def set_points(guild_id: int, user_id: int, points: int):
    # users ต้อง unique (guild_id, user_id)
    sb_sync(
        "set_points",
        lambda: supabase.table("users").upsert(
            {"guild_id": guild_id, "user_id": user_id, "points": int(points)},
            on_conflict="guild_id,user_id"
        ).execute()
    )

def add_points(guild_id: int, user_id: int, amount: int):
    before = get_points(guild_id, user_id)
    after = before + int(amount)
    set_points(guild_id, user_id, after)
    return before, after

def can_claim_daily(guild_id: int, user_id: int) -> bool:
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    res = sb_sync(
        "can_claim_daily",
        lambda: supabase.table("users").select("last_daily").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    )
    if not res or not getattr(res, "data", None):
        return True
    return (res.data[0].get("last_daily") != today)

def set_daily_claimed(guild_id: int, user_id: int):
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    sb_sync(
        "set_daily_claimed",
        lambda: supabase.table("users").update({"last_daily": today}).eq("guild_id", guild_id).eq("user_id", user_id).execute()
    )

def list_voice_channels(guild_id: int):
    res = sb_sync(
        "list_voice_channels",
        lambda: supabase.table("voice_channels").select("channel_id").eq("guild_id", guild_id).execute()
    )
    if not res or not getattr(res, "data", None):
        return []
    ids = []
    for r in res.data:
        try:
            ids.append(int(r["channel_id"]))
        except Exception:
            pass
    return ids

def get_or_create_voice_progress(guild_id: int, user_id: int):
    res = sb_sync(
        "get_voice_progress",
        lambda: supabase.table("voice_progress").select("*").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    )
    if not res or not getattr(res, "data", None):
        new_row = {
            "guild_id": guild_id,
            "user_id": user_id,
            "active_minutes": 0,
            "muted_streak_minutes": 0,
            "channel_id": None,
            "last_tick_utc": None,
        }
        sb_sync(
            "voice_progress insert",
            lambda: supabase.table("voice_progress").insert(new_row).execute()
        )
        return new_row
    return res.data[0]

def update_voice_progress(guild_id: int, user_id: int, channel_id, active_minutes: int, muted_streak: int):
    # voice_progress ต้อง unique (guild_id, user_id)
    sb_sync(
        "update_voice_progress",
        lambda: supabase.table("voice_progress").upsert({
            "guild_id": guild_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "active_minutes": int(active_minutes),
            "muted_streak_minutes": int(muted_streak),
            "last_tick_utc": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="guild_id,user_id").execute()
    )


# ======================
# Logging Helpers
# ======================
async def send_log(guild: discord.Guild, key: str, text: str):
    try:
        ch_id = get_setting(guild.id, key)
        if not ch_id:
            return
        ch = guild.get_channel(int(ch_id))
        if ch:
            await ch.send(text)
    except Exception as e:
        print("[LOG-ERROR]", e)


# ======================
# UI Views
# ======================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ กดรับ Daily", style=discord.ButtonStyle.success, custom_id="aura:daily")
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("ใช้ได้เฉพาะในเซิร์ฟเวอร์นะ", ephemeral=True)

            gid, uid = interaction.guild.id, interaction.user.id
            amt = int(get_setting(gid, "daily_amount", DEFAULT_DAILY_AMOUNT))

            if not can_claim_daily(gid, uid):
                pts = get_points(gid, uid)
                return await interaction.response.send_message(
                    f"วันนี้รับไปแล้วน้า 😆\nคะแนนตอนนี้: **{pts}** แต้ม",
                    ephemeral=True
                )

            before, after = add_points(gid, uid, amt)
            set_daily_claimed(gid, uid)

            await interaction.response.send_message(
                f"รับ Daily แล้ว ✅ +{amt} แต้ม\nคะแนน: **{before} → {after}**",
                ephemeral=True
            )
            await send_log(
                interaction.guild,
                "daily_log_channel_id",
                f"🟩 **DAILY CLAIM**\n👤 ผู้เล่น: <@{uid}>\n➕ ได้รับ: +{amt} แต้ม\n📊 คะแนน: {before} → {after}"
            )
        except Exception as e:
            print("[DAILY-ERROR]", e)
            if not interaction.response.is_done():
                await interaction.response.send_message("เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ", ephemeral=True)


class RollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 สุ่มรางวัล", style=discord.ButtonStyle.danger, custom_id="aura:roll")
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("ใช้ได้เฉพาะในเซิร์ฟเวอร์นะ", ephemeral=True)

            gid, uid = interaction.guild.id, interaction.user.id
            cost = int(get_setting(gid, "roll_cost", DEFAULT_ROLL_COST))

            pts_before = get_points(gid, uid)
            if pts_before < cost:
                return await interaction.response.send_message(
                    f"แต้มไม่พอจ้า 😅 ต้องใช้ {cost} แต้ม\nมีอยู่: **{pts_before}**",
                    ephemeral=True
                )

            # หักแต้มก่อน
            set_points(gid, uid, pts_before - cost)

            total = sum(r["rate"] for r in GACHA_REWARDS)
            pick = random.uniform(0, total)
            cur = 0.0
            reward = GACHA_REWARDS[-1]["name"]
            for r in GACHA_REWARDS:
                cur += r["rate"]
                if pick <= cur:
                    reward = r["name"]
                    break

            pts_after = get_points(gid, uid)

            await interaction.response.send_message(
                f"🎉 ได้รางวัล: **{reward}**\n💰คงเหลือ: **{pts_after}**\n📸 แคปรูปยืนยันด้วยนะ",
                ephemeral=True
            )
            await send_log(
                interaction.guild,
                "gacha_log_channel_id",
                f"🎲 **AURA GACHA**\n👤 ผู้เล่น: <@{uid}>\n🎁 รางวัล: **{reward}**\n💸 ใช้แต้ม: -{cost}\n📊 คะแนน: {pts_before} → {pts_after}"
            )
        except Exception as e:
            print("[GACHA-ERROR]", e)
            if not interaction.response.is_done():
                await interaction.response.send_message("เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ", ephemeral=True)

    @discord.ui.button(label="📊 เช็คคะแนน", style=discord.ButtonStyle.secondary, custom_id="aura:checkpoints")
    async def checkpoints_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("ใช้ได้เฉพาะในเซิร์ฟเวอร์นะ", ephemeral=True)
            pts = get_points(interaction.guild.id, interaction.user.id)
            await interaction.response.send_message(f"คะแนนของคุณตอนนี้: **{pts}** แต้ม ✅", ephemeral=True)
        except Exception as e:
            print("[CHECKPOINTS-ERROR]", e)
            if not interaction.response.is_done():
                await interaction.response.send_message("เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ", ephemeral=True)


# ======================
# Admin Commands
# ======================
@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicerewardminutes(ctx, minutes: int):
    set_setting(ctx.guild.id, "voice_reward_minutes", str(minutes))
    await ctx.send(f"ตั้งค่า Voice Reward = ครบ **{minutes}** นาที ได้แต้ม ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicerewardpoints(ctx, points: int):
    set_setting(ctx.guild.id, "voice_reward_points", str(points))
    await ctx.send(f"ตั้งค่า Voice Reward = ได้รับ **{points}** แต้ม ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def setvoicemutelimit(ctx, minutes: int):
    set_setting(ctx.guild.id, "voice_mute_limit_min", str(minutes))
    await ctx.send(f"ตั้งค่า Mute Limit = ปิดไมค์เกิน **{minutes}** นาที จะรีเซ็ตเวลา ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def addvoicechannel(ctx, channel: discord.VoiceChannel):
    # voice_channels ควร unique (guild_id, channel_id)
    sb_sync(
        "addvoicechannel",
        lambda: supabase.table("voice_channels").upsert(
            {"guild_id": ctx.guild.id, "channel_id": channel.id},
            on_conflict="guild_id,channel_id"
        ).execute()
    )
    await ctx.send(f"เพิ่มห้องเสียง {channel.mention} แล้ว ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def listvoicechannels(ctx):
    ids = list_voice_channels(ctx.guild.id)
    names = [ctx.guild.get_channel(cid).mention if ctx.guild.get_channel(cid) else f"`{cid}`" for cid in ids]
    await ctx.send("🔊 ห้องเสียงที่นับแต้ม:\n" + ("\n".join(names) if names else "ไม่มี"))

@bot.command()
@commands.has_permissions(administrator=True)
async def setpoint(ctx, member: discord.Member, amount: int):
    set_points(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ ตั้งค่าแต้ม {member.mention} เป็น **{amount}**")

@bot.command()
@commands.has_permissions(administrator=True)
async def givepoint(ctx, member: discord.Member, amount: int):
    before, after = add_points(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ เพิ่มแต้ม {member.mention}: {before} -> {after}")

@bot.command()
async def points(ctx):
    pts = get_points(ctx.guild.id, ctx.author.id)
    await ctx.send(f"<@{ctx.author.id}> ตอนนี้มี **{pts}** แต้ม ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def setupgacha(ctx):
    cost = int(get_setting(ctx.guild.id, "roll_cost", DEFAULT_ROLL_COST))
    embed = discord.Embed(
        title="AURA GACHA",
        description=f"กดสุ่มรางวัล ใช้ **{cost}** แต้ม/ครั้ง",
        color=0xFF0033
    )
    await ctx.send(embed=embed, view=RollView())

@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx):
    amt = int(get_setting(ctx.guild.id, "daily_amount", DEFAULT_DAILY_AMOUNT))
    embed = discord.Embed(
        title="DAILY CLAIM",
        description=f"รับได้วันละครั้ง (+{amt} แต้ม)",
        color=0x5865F2
    )
    await ctx.send(embed=embed, view=DailyView())

@bot.command()
@commands.has_permissions(administrator=True)
async def pingbot(ctx):
    await ctx.send(f"pong ✅ latency {round(bot.latency*1000)}ms")


# ======================
# Command Error Handler (กันคำสั่งเงียบ)
# ======================
@bot.event
async def on_command_error(ctx, error):
    print("[CMD-ERROR]", repr(error))
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("❌ คำสั่งนี้ต้องเป็นแอดมินนะ")
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"❌ error: `{type(error).__name__}`")


# ======================
# Voice tracking loop (เสถียร + ไม่หนัก)
# ======================
def is_member_effectively_muted(member: discord.Member) -> bool:
    vs = member.voice
    return (not vs) or any([vs.self_mute, vs.self_deaf, vs.mute, vs.deaf])

# cache ลดการยิง DB
_voice_cache = {
    "allowed": {},  # guild_id -> (ts, set(channel_ids))
    "settings": {}, # guild_id -> (ts, reward_minutes, reward_points, mute_limit)
}
CACHE_SECONDS = 30

def _get_cached_allowed(guild_id: int):
    now = datetime.now(timezone.utc).timestamp()
    item = _voice_cache["allowed"].get(guild_id)
    if item and (now - item[0]) < CACHE_SECONDS:
        return item[1]
    allowed = set(list_voice_channels(guild_id))
    _voice_cache["allowed"][guild_id] = (now, allowed)
    return allowed

def _get_cached_settings(guild_id: int):
    now = datetime.now(timezone.utc).timestamp()
    item = _voice_cache["settings"].get(guild_id)
    if item and (now - item[0]) < CACHE_SECONDS:
        return item[1], item[2], item[3]

    reward_min_raw = get_setting(guild_id, "voice_reward_minutes")
    reward_minutes = int(reward_min_raw) if reward_min_raw else DEFAULT_VOICE_REWARD_MINUTES

    reward_pts_raw = get_setting(guild_id, "voice_reward_points")
    reward_points = int(reward_pts_raw) if reward_pts_raw else DEFAULT_VOICE_REWARD_POINTS

    mute_limit_raw = get_setting(guild_id, "voice_mute_limit_min")
    mute_limit = int(mute_limit_raw) if mute_limit_raw else DEFAULT_VOICE_MUTE_LIMIT_MIN

    _voice_cache["settings"][guild_id] = (now, reward_minutes, reward_points, mute_limit)
    return reward_minutes, reward_points, mute_limit

@tasks.loop(minutes=1)
async def voice_tick():
    try:
        for guild in bot.guilds:
            allowed = _get_cached_allowed(guild.id)
            if not allowed:
                continue

            reward_minutes, reward_points, mute_limit = _get_cached_settings(guild.id)

            # ✅ วนเฉพาะคนที่อยู่ในห้องที่อนุญาต (ไม่วนทั้งกิลด์)
            voice_members = []
            for ch_id in allowed:
                ch = guild.get_channel(ch_id)
                if ch and hasattr(ch, "members"):
                    voice_members.extend([m for m in ch.members if not m.bot])

            # ไม่มีคนในห้องเสียงที่อนุญาต ก็จบ
            if not voice_members:
                continue

            for member in voice_members:
                vs = member.voice
                if not vs or not vs.channel:
                    continue

                row = get_or_create_voice_progress(guild.id, member.id)
                active = int(row.get("active_minutes") or 0)
                muted_streak = int(row.get("muted_streak_minutes") or 0)

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
                    active = 0
                    try:
                        await member.send(
                            f"🎧 คุณอยู่ห้องเสียงครบ {reward_minutes} นาทีแล้ว!\n"
                            f"ได้รับ +{reward_points} แต้ม ✅\n"
                            f"คะแนน: {before} → {after}"
                        )
                    except Exception:
                        pass

                update_voice_progress(guild.id, member.id, vs.channel.id, active, muted_streak)

    except Exception as e:
        # กัน loop หลุดแล้ว task ตาย
        print("[VOICE_TICK ERROR]", type(e).__name__, e)

@voice_tick.before_loop
async def before_voice_tick():
    await bot.wait_until_ready()


# ======================
# Bot Ready
# ======================
@bot.event
async def on_ready():
    print(f"✅ บอท {bot.user} พร้อมใช้งาน (Supabase Cloud Mode)")
    for g in bot.guilds:
        try:
            print("GUILD:", g.name, "members_cached:", len(g.members))
        except Exception:
            pass

    # persistent views
    bot.add_view(RollView())
    bot.add_view(DailyView())

    if not voice_tick.is_running():
        voice_tick.start()


def main():
    if server_on:
        server_on()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
