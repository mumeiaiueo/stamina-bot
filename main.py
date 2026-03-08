import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from supabase import create_client, Client

UTC = timezone.utc
JST = timezone(timedelta(hours=9))

TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN が未設定です")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL が未設定です")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY が未設定です")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_STOCK_DEFAULT = 5
RECOVER_MINUTES_DEFAULT = 180  # 3時間

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_iso_to_utc(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_jst_text(dt):
    if dt is None:
        return "未使用"
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def calc_stock(last_used_at, max_stock, recover_minutes):
    if last_used_at is None:
        return max_stock
    elapsed_sec = (utc_now() - last_used_at).total_seconds()
    recovered = int(elapsed_sec // (recover_minutes * 60))
    return max(0, min(max_stock, recovered))


def next_recovery_at(last_used_at, max_stock, recover_minutes):
    if last_used_at is None:
        return None
    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    if stock >= max_stock:
        return None
    return last_used_at + timedelta(minutes=(stock + 1) * recover_minutes)


def full_recovery_at(last_used_at, max_stock, recover_minutes):
    if last_used_at is None:
        return None
    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    if stock >= max_stock:
        return None
    remain = max_stock - stock
    return utc_now() + timedelta(minutes=remain * recover_minutes)


class StaminaRepo:
    def __init__(self):
        self._locks = {}

    def get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def _db(self, fn):
        return await asyncio.to_thread(fn)

    async def get_panel(self, channel_id: int):
        def work():
            return sb.table("stamina_panels").select("*").eq("channel_id", channel_id).limit(1).execute()
        res = await self._db(work)
        rows = res.data or []
        return rows[0] if rows else None

    async def upsert_panel(self, guild_id: int, channel_id: int, panel_message_id=None, log_channel_id=None):
        payload = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "panel_message_id": panel_message_id,
            "log_channel_id": log_channel_id,
            "max_stock": MAX_STOCK_DEFAULT,
            "recover_minutes": RECOVER_MINUTES_DEFAULT,
            "updated_at": utc_now().isoformat(),
        }

        def work():
            return sb.table("stamina_panels").upsert(payload).execute()
        return await self._db(work)

    async def set_panel_message_id(self, channel_id: int, panel_message_id: int):
        def work():
            return sb.table("stamina_panels").update({
                "panel_message_id": panel_message_id,
                "updated_at": utc_now().isoformat()
            }).eq("channel_id", channel_id).execute()
        return await self._db(work)

    async def set_log_channel(self, channel_id: int, log_channel_id: int | None):
        def work():
            return sb.table("stamina_panels").update({
                "log_channel_id": log_channel_id,
                "updated_at": utc_now().isoformat()
            }).eq("channel_id", channel_id).execute()
        return await self._db(work)

    async def set_last_used_now(self, channel_id: int):
        def work():
            return sb.table("stamina_panels").update({
                "last_used_at": utc_now().isoformat(),
                "updated_at": utc_now().isoformat()
            }).eq("channel_id", channel_id).execute()
        return await self._db(work)

    async def set_full(self, channel_id: int):
        def work():
            return sb.table("stamina_panels").update({
                "last_used_at": None,
                "updated_at": utc_now().isoformat()
            }).eq("channel_id", channel_id).execute()
        return await self._db(work)


repo = StaminaRepo()


def build_embed(row: dict) -> discord.Embed:
    max_stock = int(row.get("max_stock") or MAX_STOCK_DEFAULT)
    recover_minutes = int(row.get("recover_minutes") or RECOVER_MINUTES_DEFAULT)
    last_used_at = parse_iso_to_utc(row.get("last_used_at"))

    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    next_at = next_recovery_at(last_used_at, max_stock, recover_minutes)
    full_at = full_recovery_at(last_used_at, max_stock, recover_minutes)

    bars = "🟩" * stock + "⬜" * (max_stock - stock)

    lines = [
        f"**現在残数**: {stock}/{max_stock}",
        f"**表示**: {bars}",
        f"**回復**: 3時間ごとに1回復",
        f"**最後に使用**: {to_jst_text(last_used_at)}",
        f"**次回復**: {to_jst_text(next_at) if next_at else 'なし（MAX）'}",
        f"**全回復予定**: {to_jst_text(full_at) if full_at else '済'}",
    ]

    embed = discord.Embed(
        title="回復パネル",
        description="\n".join(lines)
    )
    embed.set_footer(text="1回使うと残数は0になります")
    return embed


async def send_log(guild: discord.Guild, row: dict, user: discord.Member | discord.User, used_channel, before_stock: int):
    log_channel_id = row.get("log_channel_id")
    if not log_channel_id:
        return

    log_channel = guild.get_channel(int(log_channel_id))
    if log_channel is None:
        try:
            log_channel = await guild.fetch_channel(int(log_channel_id))
        except Exception:
            return

    embed = discord.Embed(
        title="回復使用ログ",
        description=(
            f"**ユーザー**: {user.mention} (`{user.id}`)\n"
            f"**チャンネル**: {getattr(used_channel, 'mention', '不明')}\n"
            f"**使用前残数**: {before_stock}\n"
            f"**使用後残数**: 0\n"
            f"**時刻**: {to_jst_text(utc_now())}"
        )
    )
    try:
        await log_channel.send(embed=embed)
    except Exception:
        pass


class RecoveryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="使用する", style=discord.ButtonStyle.danger, custom_id="recovery_use_zero")
    async def use_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        channel_id = interaction.channel.id
        lock = repo.get_lock(channel_id)

        async with lock:
            row = await repo.get_panel(channel_id)
            if not row:
                await interaction.response.send_message("このチャンネルは未設定です。", ephemeral=True)
                return

            max_stock = int(row.get("max_stock") or MAX_STOCK_DEFAULT)
            recover_minutes = int(row.get("recover_minutes") or RECOVER_MINUTES_DEFAULT)
            last_used_at = parse_iso_to_utc(row.get("last_used_at"))
            before_stock = calc_stock(last_used_at, max_stock, recover_minutes)

            if before_stock <= 0:
                nxt = next_recovery_at(last_used_at, max_stock, recover_minutes)
                msg = "まだ使えません。"
                if nxt:
                    msg += f"\n次回復: {to_jst_text(nxt)}"
                await interaction.response.send_message(msg, ephemeral=True)
                return

            await repo.set_last_used_now(channel_id)
            row = await repo.get_panel(channel_id)

            await interaction.response.edit_message(embed=build_embed(row), view=RecoveryView())
            await send_log(interaction.guild, row, interaction.user, interaction.channel, before_stock)


async def refresh_panel(channel: discord.TextChannel):
    row = await repo.get_panel(channel.id)
    if not row:
        return False

    message_id = row.get("panel_message_id")
    if not message_id:
        return False

    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        return False

    await msg.edit(embed=build_embed(row), view=RecoveryView())
    return True


@bot.event
async def on_ready():
    bot.add_view(RecoveryView())
    try:
        synced = await bot.tree.sync()
        print(f"✅ commands synced: {len(synced)}")
    except Exception as e:
        print("❌ sync error:", e)

    print(f"✅ Logged in as {bot.user} ({bot.user.id})")


@bot.tree.command(name="stamina_setup", description="このチャンネルに回復パネルを設置")
@app_commands.describe(log_channel="管理者ログ送信先")
async def stamina_setup(interaction: discord.Interaction, log_channel: discord.TextChannel | None = None):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("テキストチャンネルで使ってください。", ephemeral=True)
        return

    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    await repo.upsert_panel(
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        panel_message_id=None,
        log_channel_id=log_channel.id if log_channel else None
    )

    row = await repo.get_panel(interaction.channel.id)
    await interaction.response.send_message("回復パネルを作成しました。", ephemeral=True)
    msg = await interaction.channel.send(embed=build_embed(row), view=RecoveryView())
    await repo.set_panel_message_id(interaction.channel.id, msg.id)


@bot.tree.command(name="stamina_status", description="現在の状態を確認")
async def stamina_status(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    row = await repo.get_panel(interaction.channel.id)
    if not row:
        await interaction.response.send_message("このチャンネルは未設定です。", ephemeral=True)
        return

    await interaction.response.send_message(embed=build_embed(row), ephemeral=True)


@bot.tree.command(name="stamina_refresh", description="パネルを手動更新")
async def stamina_refresh(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("テキストチャンネルで使ってください。", ephemeral=True)
        return

    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    ok = await refresh_panel(interaction.channel)
    await interaction.response.send_message("更新しました。" if ok else "更新失敗です。", ephemeral=True)


@bot.tree.command(name="stamina_logset", description="ログ送信先を設定")
@app_commands.describe(log_channel="ログ送信先")
async def stamina_logset(interaction: discord.Interaction, log_channel: discord.TextChannel):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    row = await repo.get_panel(interaction.channel.id)
    if not row:
        await interaction.response.send_message("先に /stamina_setup をしてください。", ephemeral=True)
        return

    await repo.set_log_channel(interaction.channel.id, log_channel.id)
    await interaction.response.send_message(f"ログ送信先を {log_channel.mention} に設定しました。", ephemeral=True)


@bot.tree.command(name="stamina_full", description="全回復にする")
async def stamina_full(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    row = await repo.get_panel(interaction.channel.id)
    if not row:
        await interaction.response.send_message("このチャンネルは未設定です。", ephemeral=True)
        return

    await repo.set_full(interaction.channel.id)
    if isinstance(interaction.channel, discord.TextChannel):
        await refresh_panel(interaction.channel)

    await interaction.response.send_message("全回復にしました。", ephemeral=True)


bot.run(TOKEN)