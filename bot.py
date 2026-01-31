import os
import random
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

# ดึงค่าจาก .env หรือจะกรอกตรงๆ ก็ได้ครับ
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://jkkjedrifaryttaqwyrc.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_secret_gMR0dz0GYGftueiGcYftwQ_irlu8hIb")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DEFAULT_DAILY_AMOUNT = 10
DEFAULT_ROLL_COST = 10

DEFAULT_VOICE_REWARD_MINUTES = 60
DEFAULT_VOICE_REWARD_POINTS = 10
DEFAULT_VOICE_CHECK_EVERY_MIN = 1
DEFAULT_VOICE_MUTE_LIMIT_MIN = 30

# กาชา (หน้าตาเหมือนเดิม)
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
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ======================
# DB helpers (เปลี่ยนจาก SQLite เป็น Supabase)
# ======================
def set_setting(guild_id: int, key: str, value: str):
    supabase.table("settings").upsert({
        "guild_id": guild_id, "key": key, "value": str(value)
    }).execute()

def get_setting(guild_id: int, key: str, default=None):
    res = supabase.table("settings").select("value").eq("guild_id", guild_id).eq("key", key).execute()
    return res.data[0]["value"] if res.data else default

def get_points(guild_id: int, user_id: int) -> int:
    res = supabase.table("users").select("points").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    if not res.data:
        supabase.table("users").insert({"guild_id": guild_id, "user_id": user_id, "points": 0}).execute()
        return 0
    return int(res.data[0]["points"])

def set_points(guild_id: int, user_id: int, points: int):
    supabase.table("users").upsert({
        "guild_id": guild_id, "user_id": user_id, "points": int(points)
    }).execute()

def add_points(guild_id: int, user_id: int, amount: int):
    before = get_points(guild_id, user_id)
    after = before + int(amount)
    set_points(guild_id, user_id, after)
    return before, after

def can_claim_daily(guild_id: int, user_id: int) -> bool:
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    res = supabase.table("users").select("last_daily").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    if not res.data: return True
    return res.data[0].get("last_daily") != today

def set_daily_claimed(guild_id: int, user_id: int):
    today = datetime.now(TH_TZ).strftime("%Y-%m-%d")
    supabase.table("users").update({"last_daily": today}).eq("guild_id", guild_id).eq("user_id", user_id).execute()

def add_voice_channel(guild_id: int, channel_id: int):
    supabase.table("voice_channels").upsert({"guild_id": guild_id, "channel_id": channel_id}).execute()

def remove_voice_channel(guild_id: int, channel_id: int):
    supabase.table("voice_channels").delete().eq("guild_id", guild_id).eq("channel_id", channel_id).execute()

def list_voice_channels(guild_id: int):
    res = supabase.table("voice_channels").select("channel_id").eq("guild_id", guild_id).execute()
    return [int(r["channel_id"]) for r in res.data]

def get_or_create_voice_progress(guild_id: int, user_id: int):
    res = supabase.table("voice_progress").select("*").eq("guild_id", guild_id).eq("user_id", user_id).execute()
    if not res.data:
        new_row = {"guild_id": guild_id, "user_id": user_id, "active_minutes": 0, "muted_streak_minutes": 0}
        supabase.table("voice_progress").insert(new_row).execute()
        return new_row
    return res.data[0]

def update_voice_progress(guild_id: int, user_id: int, channel_id, active_minutes: int, muted_streak: int):
    supabase.table("voice_progress").upsert({
        "guild_id": guild_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "active_minutes": int(active_minutes),
        "muted_streak_minutes": int(muted_streak),
        "last_tick_utc": datetime.now(timezone.utc).isoformat()
    }).execute()

# ======================
# Logging & Gacha helper
# ======================
async def _send_log_to_channel_id(guild: discord.Guild, channel_id, text: str):
    if not guild or not channel_id: return
    ch = guild.get_channel(int(channel_id))
    if ch: await ch.send(text)

async def send_daily_log(guild: discord.Guild, text: str):
    ch_id = get_setting(guild.id, "daily_log_channel_id")
    await _send_log_to_channel_id(guild, ch_id, text)

async def send_gacha_log(guild: discord.Guild, text: str):
    ch_id = get_setting(guild.id, "gacha_log_channel_id")
    await _send_log_to_channel_id(guild, ch_id, text)

def roll_reward_name() -> str:
    total = sum(r["rate"] for r in GACHA_REWARDS)
    pick = random.uniform(0, total)
    cur = 0.0
    for r in GACHA_REWARDS:
        cur += r["rate"]
        if pick <= cur: return r["name"]
    return GACHA_REWARDS[-1]["name"]

# ======================
# UI Views (คงหน้าตาเหมือนเดิมทุกประการ)
# ======================
class DailyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ กดรับ Daily", style=discord.ButtonStyle.success, custom_id="aura:daily")
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid, uid = interaction.guild.id, interaction.user.id
        daily_amount = int(get_setting(gid, "daily_amount", DEFAULT_DAILY_AMOUNT))
        pts_before = get_points(gid, uid)

        if not can_claim_daily(gid, uid):
            await interaction.response.send_message(f"วันนี้รับไปแล้วน้า 😆\nคะแนนตอนนี้: **{pts_before}** แต้ม", ephemeral=True)
            return

        before, after = add_points(gid, uid, daily_amount)
        set_daily_claimed(gid, uid)
        await interaction.response.send_message(f"รับ Daily แล้ว ✅ +{daily_amount} แต้ม\nคะแนน: **{before} → {after}**", ephemeral=True)
        await send_daily_log(interaction.guild, f"🟩 **DAILY CLAIM**\n👤 ผู้เล่น: <@{uid}>\n➕ ได้รับ: +{daily_amount} แต้ม\n📊 คะแนน: {before} → {after}")

class RollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 สุ่มรางวัล", style=discord.ButtonStyle.danger, custom_id="aura:roll")
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid, uid = interaction.guild.id, interaction.user.id
        roll_ch = get_setting(gid, "roll_channel_id")
        if roll_ch and int(roll_ch) != interaction.channel_id:
            return await interaction.response.send_message("ปุ่มสุ่มรางวัลใช้ได้เฉพาะห้องที่ตั้งไว้นะ 💜", ephemeral=True)

        roll_cost = int(get_setting(gid, "roll_cost", DEFAULT_ROLL_COST))
        pts_before = get_points(gid, uid)

        if pts_before < roll_cost:
            return await interaction.response.send_message(f"แต้มไม่พอจ้า 😅 ต้องใช้ {roll_cost} แต้ม/ครั้ง\nตอนนี้คุณมี: **{pts_before}** แต้ม", ephemeral=True)

        set_points(gid, uid, pts_before - roll_cost)
        reward = roll_reward_name()
        pts_after = get_points(gid, uid)

        await interaction.response.send_message(f"🎉 คุณได้รางวัล: **{reward}**\n💰แต้มคงเหลือ: **{pts_after}**\n\n📸 **หลังจากคุณกดสุ่มแล้วให้แคปรูปเพื่อเป็นหลักฐานในการยืนยันเพื่อรับของรางวัลของ**", ephemeral=True)
        await send_gacha_log(interaction.guild, f"🎲 **AURA GACHA**\n👤 ผู้เล่น: <@{uid}>\n🎁 รางวัล: **{reward}**\n💸 ใช้แต้ม: -{roll_cost}\n📊 คะแนน: {pts_before} → {pts_after}\n-------------------------------------------")

    @discord.ui.button(label="📊 เช็คคะแนน", style=discord.ButtonStyle.secondary, custom_id="aura:checkpoints")
    async def checkpoints_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pts = get_points(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"คะแนนของคุณตอนนี้: **{pts}** แต้ม ✅", ephemeral=True)

# ======================
# Commands
# ======================
@bot.command()
async def points(ctx: commands.Context):
    pts = get_points(ctx.guild.id, ctx.author.id)
    await ctx.send(f"<@{ctx.author.id}> ตอนนี้มี **{pts}** แต้ม ✅")

@bot.command()
@commands.has_permissions(administrator=True)
async def givepoint(ctx: commands.Context, member: discord.Member, amount: int):
    before, after = add_points(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ เพิ่มแต้มให้ {member.mention} แล้ว: **{before} → {after}**")

@bot.command()
@commands.has_permissions(administrator=True)
async def setpoint(ctx: commands.Context, member: discord.Member, amount: int):
    set_points(ctx.guild.id, member.id, amount)
    await ctx.send(f"✅ ตั้งค่าแต้ม {member.mention} ใหม่เป็น **{amount}** แต้ม!")

@bot.command()
@commands.has_permissions(administrator=True)
async def setupgacha(ctx: commands.Context):
    cost = get_setting(ctx.guild.id, "roll_cost", DEFAULT_ROLL_COST)
    embed = discord.Embed(title="AURA GACHA", description=f"กดสุ่มรางวัล ใช้ **{cost}** แต้ม/ครั้ง\n\n**กดสุ่มแล้วให้แคปรูปเพื่อเป็นหลักฐานในการรับของรางวัล**", color=0xFF0033)
    img = get_setting(ctx.guild.id, "gacha_image_url")
    if img: embed.set_image(url=img)
    await ctx.send(embed=embed, view=RollView())

@bot.command()
@commands.has_permissions(administrator=True)
async def setupdaily(ctx: commands.Context):
    amt = get_setting(ctx.guild.id, "daily_amount", DEFAULT_DAILY_AMOUNT)
    embed = discord.Embed(title="DAILY CLAIM", description=f"สามารถกดรับ Daily ได้ 1 ครั้งต่อวันเท่านั้น (**+{amt}** แต้ม)", color=0x5865F2)
    img = get_setting(ctx.guild.id, "daily_image_url")
    if img: embed.set_image(url=img)
    await ctx.send(embed=embed, view=DailyView())

# --- Log/Setup commands ---
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
async def addvoicechannel(ctx: commands.Context, channel: discord.VoiceChannel):
    add_voice_channel(ctx.guild.id, channel.id)
    await ctx.send(f"เพิ่มห้องเสียง {channel.mention} แล้ว ✅")

# (และคำสั่งอื่นๆ เช่น setrollcost, setdailyamount... สามารถเพิ่มได้เหมือนเดิม)

# ======================
# Voice tracking loop
# ======================
def is_member_effectively_muted(member: discord.Member) -> bool:
    vs = member.voice
    return not vs or any([vs.self_mute, vs.self_deaf, vs.mute, vs.deaf])

@tasks.loop(minutes=1)
async def voice_tick():
    for guild in bot.guilds:
        allowed = set(list_voice_channels(guild.id))
        if not allowed: continue

        reward_minutes = int(get_setting(guild.id, "voice_reward_minutes", DEFAULT_VOICE_REWARD_MINUTES))
        reward_points = int(get_setting(guild.id, "voice_reward_points", DEFAULT_VOICE_REWARD_POINTS))
        mute_limit = int(get_setting(guild.id, "voice_mute_limit_min", DEFAULT_VOICE_MUTE_LIMIT_MIN))

        for member in guild.members:
            if member.bot: continue
            vs = member.voice
            if not vs or not vs.channel or vs.channel.id not in allowed:
                row = get_or_create_voice_progress(guild.id, member.id)
                if row["active_minutes"] != 0 or row["channel_id"] is not None:
                    update_voice_progress(guild.id, member.id, None, 0, 0)
                continue

            row = get_or_create_voice_progress(guild.id, member.id)
            active, muted_streak = int(row["active_minutes"]), int(row["muted_streak_minutes"])

            if is_member_effectively_muted(member):
                muted_streak += 1
                if muted_streak >= mute_limit: active = 0
                update_voice_progress(guild.id, member.id, vs.channel.id, active, muted_streak)
                continue

            muted_streak, active = 0, active + 1
            if active >= reward_minutes:
                before, after = add_points(guild.id, member.id, reward_points)
                active -= reward_minutes
                try: await member.send(f"🎧 คุณอยู่ห้องเสียงครบ {reward_minutes} นาทีแล้ว!\nได้รับ +{reward_points} แต้ม ✅\nคะแนน: {before} → {after}")
                except: pass

            update_voice_progress(guild.id, member.id, vs.channel.id, active, muted_streak)

@voice_tick.before_loop
async def before_voice_tick(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"✅ บอท {bot.user} พร้อมใช้งาน (Supabase Cloud Mode)")
    bot.add_view(RollView())
    bot.add_view(DailyView())
    if not voice_tick.is_running(): voice_tick.start()

def main():
    if server_on: server_on()
    token = os.getenv("DISCORD_TOKEN")
    if not token: raise RuntimeError("❌ ไม่พบ DISCORD_TOKEN")
    bot.run(token)

if __name__ == "__main__":
    main()
