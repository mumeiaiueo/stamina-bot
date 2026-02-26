import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from aiohttp import web
import psycopg

# =========================
# 設定
# =========================
MAX_CHARGES = 5
RECOVER_EVERY = timedelta(hours=3)

TOKEN = os.environ.get("DISCORD_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です")

DATABASE_URL = re.sub(r"^postgres://", "postgresql://", DATABASE_URL)

TABLE = "stamina_scoped"  # ←チャンネル別にする新テーブル（既存と衝突回避）


# =========================
# 時刻処理
# =========================
def utcnow():
    return datetime.now(timezone.utc)


def calc_recovered(charges, last_tick, now):
    if charges >= MAX_CHARGES:
        return charges, last_tick

    elapsed = now - last_tick
    add = int(elapsed.total_seconds() // RECOVER_EVERY.total_seconds())
    if add <= 0:
        return charges, last_tick

    new_charges = min(MAX_CHARGES, charges + add)
    new_last = last_tick + RECOVER_EVERY * add  # 余り時間保持
    return new_charges, new_last


def next_recover_text(charges, last_tick, now):
    if charges >= MAX_CHARGES:
        return "満タン"

    elapsed = now - last_tick
    mod = elapsed.total_seconds() % RECOVER_EVERY.total_seconds()
    remain = RECOVER_EVERY.total_seconds() - mod

    mins = int(remain // 60)
    h = mins // 60
    m = mins % 60
    return f"{h:02d}:{m:02d} 後"


# =========================
# DB処理（user_id × channel_id）
# =========================
async def db_init():
    def work():
        with psycopg.connect(DATABASE_URL) as con:
            con.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    charges INTEGER NOT NULL,
                    last_tick_utc TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (user_id, channel_id)
                )
            """)
            con.commit()
    await asyncio.to_thread(work)


async def ensure_user(user_id: str, channel_id: str):
    now = utcnow()

    def work():
        with psycopg.connect(DATABASE_URL) as con:
            row = con.execute(
                f"SELECT charges, last_tick_utc FROM {TABLE} WHERE user_id=%s AND channel_id=%s",
                (user_id, channel_id),
            ).fetchone()

            if row is None:
                con.execute(
                    f"INSERT INTO {TABLE}(user_id, channel_id, charges, last_tick_utc) VALUES(%s,%s,%s,%s)",
                    (user_id, channel_id, 0, now),
                )
                con.commit()
                return 0, now

            charges = int(row[0])
            last_tick = row[1]
            if last_tick.tzinfo is None:
                last_tick = last_tick.replace(tzinfo=timezone.utc)

            return charges, last_tick

    return await asyncio.to_thread(work)


async def set_state(user_id: str, channel_id: str, charges: int, last_tick: datetime):
    def work():
        with psycopg.connect(DATABASE_URL) as con:
            con.execute(f"""
                INSERT INTO {TABLE}(user_id, channel_id, charges, last_tick_utc)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT (user_id, channel_id)
                DO UPDATE SET
                    charges=EXCLUDED.charges,
                    last_tick_utc=EXCLUDED.last_tick_utc
            """, (user_id, channel_id, int(charges), last_tick))
            con.commit()
    await asyncio.to_thread(work)


# =========================
# Discord UI（押したチャンネルで分岐）
# =========================
class StaminaPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def load_and_update(self, user_id: str, channel_id: str):
        charges, last_tick = await ensure_user(user_id, channel_id)
        now = utcnow()
        charges, last_tick = calc_recovered(charges, last_tick, now)
        await set_state(user_id, channel_id, charges, last_tick)
        return charges, last_tick, now

    @discord.ui.button(label="表示/更新", style=discord.ButtonStyle.secondary, custom_id="stamina:show")
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel_id)  # ←ここがチャンネル別の肝

        charges, last_tick, now = await self.load_and_update(user_id, channel_id)

        await interaction.response.send_message(
            f"📍 チャンネル: <#{channel_id}>\n"
            f"👤 {interaction.user.mention}\n"
            f"⚡ 回復回数: **{charges}/{MAX_CHARGES}**\n"
            f"⏱ 次の+1: **{next_recover_text(charges, last_tick, now)}**（3時間ごと）",
            ephemeral=True
        )

    @discord.ui.button(label="使用する（0にリセット）", style=discord.ButtonStyle.primary, custom_id="stamina:use")
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel_id)

        charges, _, _ = await self.load_and_update(user_id, channel_id)

        if charges <= 0:
            return await interaction.response.send_message("❌ 回復回数がありません（0/5）", ephemeral=True)

        now = utcnow()
        await set_state(user_id, channel_id, 0, now)

        await interaction.response.send_message(
            f"📍 チャンネル: <#{channel_id}>\n"
            f"✅ **{charges}回分**を使用して **0/5** にリセットしました。\n"
            f"⏱ 次の+1は3時間後です。",
            ephemeral=True
        )


# =========================
# Bot本体
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="stamina_setup", description="このチャンネルにスタミナ管理パネルを設置（管理者）")
@app_commands.checks.has_permissions(administrator=True)
async def stamina_setup(interaction: discord.Interaction):
    await interaction.response.send_message(
        "⚡ スタミナ管理パネル（このチャンネル専用）",
        view=StaminaPanel()
    )


# =========================
# Render無料用HTTPサーバ
# =========================
async def start_web():
    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


@client.event
async def on_ready():
    await db_init()
    client.add_view(StaminaPanel())  # 再起動後もボタン有効
    await tree.sync()
    await start_web()
    print(f"Logged in as {client.user} ✅")


client.run(TOKEN)