import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from supabase import create_client, Client

UTC = timezone.utc
JST = timezone(timedelta(hours=9))

MAX_STOCK_DEFAULT = 5
RECOVER_MINUTES_DEFAULT = 180  # 3時間


# =========================================================
# ENV
# =========================================================
def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} が未設定です")
    return value


def get_env_int(name: str) -> int:
    value = get_env(name)
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} が数値ではありません: {value}")


TOKEN = get_env("BOT_TOKEN")
SUPABASE_URL = get_env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = get_env("SUPABASE_KEY")
LOG_CHANNEL_ID = get_env_int("LOG_CHANNEL_ID")

print("===== STARTUP CHECK =====")
print("BOT_TOKEN set:", bool(TOKEN))
print("SUPABASE_URL:", SUPABASE_URL)
print("SUPABASE_KEY prefix:", SUPABASE_KEY[:10] if SUPABASE_KEY else "None")
print("LOG_CHANNEL_ID:", LOG_CHANNEL_ID)

try:
    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase client created")
except Exception as e:
    print("❌ create_client error:", repr(e))
    raise


# =========================================================
# TIME / STOCK
# =========================================================
def utc_now() -> datetime:
    return datetime.now(UTC)


def jst_now() -> datetime:
    return datetime.now(JST)


def parse_iso_to_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_jst_text(dt: Optional[datetime]) -> str:
    if dt is None:
        return "未使用"
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def calc_stock(last_used_at: Optional[datetime], max_stock: int, recover_minutes: int) -> int:
    if last_used_at is None:
        return max_stock
    elapsed_sec = (utc_now() - last_used_at).total_seconds()
    recovered = int(elapsed_sec // (recover_minutes * 60))
    return max(0, min(max_stock, recovered))


def next_recovery_at(last_used_at: Optional[datetime], max_stock: int, recover_minutes: int) -> Optional[datetime]:
    if last_used_at is None:
        return None
    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    if stock >= max_stock:
        return None
    return last_used_at + timedelta(minutes=(stock + 1) * recover_minutes)


def full_recovery_at(last_used_at: Optional[datetime], max_stock: int, recover_minutes: int) -> Optional[datetime]:
    if last_used_at is None:
        return None
    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    if stock >= max_stock:
        return None
    remain = max_stock - stock
    return utc_now() + timedelta(minutes=remain * recover_minutes)


def seconds_until_next_half_hour() -> int:
    now = jst_now()
    next_run = now.replace(second=0, microsecond=0)

    if now.minute < 30:
        next_run = next_run.replace(minute=30)
    else:
        next_run = (next_run + timedelta(hours=1)).replace(minute=0)

    wait_seconds = int((next_run - now).total_seconds())
    return max(1, wait_seconds)


# =========================================================
# DISCORD
# =========================================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# =========================================================
# HELPERS
# =========================================================
def is_supported_channel(
    channel: Optional[discord.abc.GuildChannel | discord.Thread]
) -> bool:
    return isinstance(channel, (discord.TextChannel, discord.Thread))


def get_place_name(channel: Union[discord.TextChannel, discord.Thread]) -> str:
    return getattr(channel, "name", "不明")


async def get_channel_any(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None and is_supported_channel(channel):
        return channel

    try:
        channel = await bot.fetch_channel(channel_id)
        if is_supported_channel(channel):
            return channel
    except Exception as e:
        print("⚠️ get_channel_any error:", repr(e))

    return None


# =========================================================
# DB
# =========================================================
class StaminaRepo:
    def __init__(self):
        self._locks: dict[int, asyncio.Lock] = {}

    def get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def _db(self, fn):
        return await asyncio.to_thread(fn)

    async def test_connection(self):
        def work():
            return sb.table("stamina_panels").select("channel_id").limit(1).execute()
        return await self._db(work)

    async def list_panels(self):
        def work():
            return sb.table("stamina_panels").select("*").execute()
        res = await self._db(work)
        return res.data or []

    async def get_panel(self, channel_id: int):
        def work():
            return (
                sb.table("stamina_panels")
                .select("*")
                .eq("channel_id", channel_id)
                .limit(1)
                .execute()
            )
        res = await self._db(work)
        rows = res.data or []
        return rows[0] if rows else None

    async def upsert_panel(self, guild_id: int, channel_id: int, panel_message_id=None):
        payload = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "panel_message_id": panel_message_id,
            "max_stock": MAX_STOCK_DEFAULT,
            "recover_minutes": RECOVER_MINUTES_DEFAULT,
            "updated_at": utc_now().isoformat(),
        }

        def work():
            return sb.table("stamina_panels").upsert(payload).execute()

        return await self._db(work)

    async def set_panel_message_id(self, channel_id: int, panel_message_id: int):
        def work():
            return (
                sb.table("stamina_panels")
                .update({
                    "panel_message_id": panel_message_id,
                    "updated_at": utc_now().isoformat()
                })
                .eq("channel_id", channel_id)
                .execute()
            )
        return await self._db(work)

    async def set_last_used_now(self, channel_id: int):
        now_iso = utc_now().isoformat()

        def work():
            return (
                sb.table("stamina_panels")
                .update({
                    "last_used_at": now_iso,
                    "updated_at": now_iso
                })
                .eq("channel_id", channel_id)
                .execute()
            )
        return await self._db(work)

    async def set_full(self, channel_id: int):
        def work():
            return (
                sb.table("stamina_panels")
                .update({
                    "last_used_at": None,
                    "updated_at": utc_now().isoformat()
                })
                .eq("channel_id", channel_id)
                .execute()
            )
        return await self._db(work)


repo = StaminaRepo()


# =========================================================
# UI
# =========================================================
def build_embed(row: dict, place_name: str) -> discord.Embed:
    max_stock = int(row.get("max_stock") or MAX_STOCK_DEFAULT)
    recover_minutes = int(row.get("recover_minutes") or RECOVER_MINUTES_DEFAULT)
    last_used_at = parse_iso_to_utc(row.get("last_used_at"))

    stock = calc_stock(last_used_at, max_stock, recover_minutes)
    next_at = next_recovery_at(last_used_at, max_stock, recover_minutes)
    full_at = full_recovery_at(last_used_at, max_stock, recover_minutes)

    bars = "🟩" * stock + "⬜" * (max_stock - stock)

    lines = [
        f"**対象**: {place_name}",
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


async def send_log(
    user: discord.Member | discord.User,
    used_channel: Union[discord.TextChannel, discord.Thread],
):
    place_name = get_place_name(used_channel)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print("⚠️ log channel fetch error:", repr(e))
            return

    message = f"{user.mention} が **{place_name}** を使用しました"

    try:
        await log_channel.send(message)
    except Exception as e:
        print("⚠️ send_log error:", repr(e))


class RecoveryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="使用する", style=discord.ButtonStyle.danger, custom_id="recovery_use_zero")
    async def use_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.channel is None or not is_supported_channel(interaction.channel):
            await interaction.response.send_message("サーバー内のテキストチャンネル/スレッドで使ってください。", ephemeral=True)
            return

        channel = interaction.channel
        channel_id = channel.id
        lock = repo.get_lock(channel_id)

        async with lock:
            try:
                row = await repo.get_panel(channel_id)
            except Exception as e:
                print("❌ get_panel error:", repr(e))
                await interaction.response.send_message("DB取得でエラーが出ました。", ephemeral=True)
                return

            if not row:
                await interaction.response.send_message("この場所は未設定です。", ephemeral=True)
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

            try:
                await repo.set_last_used_now(channel_id)
                row = await repo.get_panel(channel_id)
            except Exception as e:
                print("❌ use_button update error:", repr(e))
                await interaction.response.send_message("使用処理でエラーが出ました。", ephemeral=True)
                return

            place_name = get_place_name(channel)
            await interaction.response.edit_message(embed=build_embed(row, place_name), view=RecoveryView())
            await send_log(interaction.user, channel)


async def refresh_panel(channel: Union[discord.TextChannel, discord.Thread]):
    row = await repo.get_panel(channel.id)
    if not row:
        return False

    message_id = row.get("panel_message_id")
    if not message_id:
        return False

    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception as e:
        print("⚠️ fetch_message error:", repr(e))
        return False

    place_name = get_place_name(channel)
    await msg.edit(embed=build_embed(row, place_name), view=RecoveryView())
    return True


async def auto_refresh_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        wait_seconds = seconds_until_next_half_hour()
        print(f"⏳ 次の自動更新まで {wait_seconds} 秒")
        await asyncio.sleep(wait_seconds)

        try:
            rows = await repo.list_panels()

            for row in rows:
                try:
                    channel_id = int(row["channel_id"])
                except Exception:
                    continue

                channel = await get_channel_any(channel_id)
                if channel is None:
                    continue

                try:
                    await refresh_panel(channel)
                except Exception as e:
                    print(f"⚠️ auto refresh failed: channel_id={channel_id}", repr(e))

                # Discordへの連続編集を少し抑える
                await asyncio.sleep(1)

            print("✅ 自動更新完了")

        except Exception as e:
            print("❌ auto_refresh_loop error:", repr(e))
            await asyncio.sleep(10)


# =========================================================
# EVENTS
# =========================================================
@bot.event
async def setup_hook():
    print("===== SETUP HOOK =====")
    try:
        await repo.test_connection()
        print("✅ Supabase connection OK")
    except Exception as e:
        print("❌ Supabase startup error:", repr(e))
        raise

    bot.add_view(RecoveryView())
    asyncio.create_task(auto_refresh_loop())


@bot.event
async def on_ready():
    print("===== ON READY =====")
    try:
        synced = await bot.tree.sync()
        print(f"✅ commands synced: {len(synced)}")
    except Exception as e:
        print("❌ sync error:", repr(e))

    print(f"✅ Logged in as {bot.user} ({bot.user.id})")


# =========================================================
# COMMANDS
# =========================================================
@bot.tree.command(name="stamina_setup", description="この場所に回復パネルを設置")
async def stamina_setup(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None or not is_supported_channel(interaction.channel):
        await interaction.response.send_message("サーバー内のテキストチャンネル/スレッドで使ってください。", ephemeral=True)
        return

    channel = interaction.channel
    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    try:
        await repo.upsert_panel(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            panel_message_id=None,
        )

        row = await repo.get_panel(channel.id)
        place_name = get_place_name(channel)

        await interaction.response.send_message("回復パネルを作成しました。", ephemeral=True)
        msg = await channel.send(embed=build_embed(row, place_name), view=RecoveryView())
        await repo.set_panel_message_id(channel.id, msg.id)

    except Exception as e:
        print("❌ stamina_setup error:", repr(e))
        if not interaction.response.is_done():
            await interaction.response.send_message("setup中にエラーが出ました。", ephemeral=True)


@bot.tree.command(name="stamina_status", description="現在の状態を確認")
async def stamina_status(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None or not is_supported_channel(interaction.channel):
        await interaction.response.send_message("サーバー内のテキストチャンネル/スレッドで使ってください。", ephemeral=True)
        return

    channel = interaction.channel

    try:
        row = await repo.get_panel(channel.id)
    except Exception as e:
        print("❌ stamina_status error:", repr(e))
        await interaction.response.send_message("状態取得でエラーが出ました。", ephemeral=True)
        return

    if not row:
        await interaction.response.send_message("この場所は未設定です。", ephemeral=True)
        return

    place_name = get_place_name(channel)
    await interaction.response.send_message(embed=build_embed(row, place_name), ephemeral=True)


@bot.tree.command(name="stamina_refresh", description="パネルを手動更新")
async def stamina_refresh(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None or not is_supported_channel(interaction.channel):
        await interaction.response.send_message("サーバー内のテキストチャンネル/スレッドで使ってください。", ephemeral=True)
        return

    channel = interaction.channel
    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    try:
        ok = await refresh_panel(channel)
    except Exception as e:
        print("❌ stamina_refresh error:", repr(e))
        await interaction.response.send_message("更新中にエラーが出ました。", ephemeral=True)
        return

    await interaction.response.send_message("更新しました。" if ok else "更新失敗です。", ephemeral=True)


@bot.tree.command(name="stamina_full", description="全回復にする")
async def stamina_full(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None or not is_supported_channel(interaction.channel):
        await interaction.response.send_message("サーバー内のテキストチャンネル/スレッドで使ってください。", ephemeral=True)
        return

    channel = interaction.channel
    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("管理者のみ使えます。", ephemeral=True)
        return

    try:
        row = await repo.get_panel(channel.id)
        if not row:
            await interaction.response.send_message("この場所は未設定です。", ephemeral=True)
            return

        await repo.set_full(channel.id)
        await refresh_panel(channel)
        await interaction.response.send_message("全回復にしました。", ephemeral=True)

    except Exception as e:
        print("❌ stamina_full error:", repr(e))
        await interaction.response.send_message("全回復処理でエラーが出ました。", ephemeral=True)


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    bot.run(TOKEN)