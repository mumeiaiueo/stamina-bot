import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from aiohttp import web

# ===== 設定 =====
MAX_CHARGES = 5
RECOVER_EVERY = timedelta(hours=3)

TOKEN = os.environ.get("DISCORD_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")  # Render Postgres（render.yamlで注入）

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です（RenderのEnvironment Variablesに入れてください）")

# ===== 時刻 =====
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

# ===== 回復計算 =====
def calc_recovered(charges: int, last_tick: datetime, now: datetime):
    if charges >= MAX_CHARGES:
        return charges, last_tick
    elapsed = now - last_tick
    add = int(elapsed.total_seconds() // RECOVER_EVERY.total_seconds())
    if add <= 0:
        return charges, last_tick
    new_charges = min(MAX_CHARGES, charges + add)
    advanced = last_tick + RECOVER_EVERY * add  # 余り時間保持
    return new_charges, advanced

def next_recover_in(charges: int, last_tick: datetime, now: datetime) -> str:
    if charges >= MAX_CHARGES:
        return "満タン"
    elapsed = now - last_tick
    mod = elapsed.total_seconds() % RECOVER_EVERY.total_seconds()
    remain = RECOVER_EVERY.total_seconds() - mod
    mins = int(remain // 60)
    h = mins // 60
    m = mins % 60
    return f"{h:02d}:{m:02d} 後"

def render_status(user: discord.abc.User, charges: int, last_tick: datetime, now: datetime) -> str:
    return (
        f"👤 {user.mention}\n"
        f"⚡ 回復回数: **{charges}/{MAX_CHARGES}**\n"
        f"⏱ 次の+1: **{next_recover_in(charges, last_tick, now)}**（3時間ごと）"
    )

# ===== DB（Postgres）=====
class PostgresStorage:
    def __init__(self, dsn: str):
        import psycopg
        self.psycopg = psycopg
        self.dsn = re.sub(r"^postgres://", "postgresql://", dsn)

    async def init(self):
        def _work():
            with self.psycopg.connect(self.dsn) as con:
                con.execute("""
                CREATE TABLE IF NOT EXISTS stamina (
                  user_id TEXT PRIMARY KEY,
                  charges INTEGER NOT NULL,
                  last_tick_utc TIMESTAMPTZ NOT NULL
                )
                """)
                con.commit()
        await asyncio.to_thread(_work)

    async def ensure_user(self, user_id: str):
        now = utcnow()
        def _work():
            with self.psycopg.connect(self.dsn) as con:
                row = con.execute(
                    "SELECT charges, last_tick_utc FROM stamina WHERE user_id=%s",
                    (user_id,)
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO stamina(user_id, charges, last_tick_utc) VALUES(%s,%s,%s)",
                        (user_id, 0, now),
                    )
                    con.commit()
                    return 0, now
                charges = int(row[0])
                last_tick = row[1]
                if last_tick.tzinfo is None:
                    last_tick = last_tick.replace(tzinfo=timezone.utc)
                return charges, last_tick
        return await asyncio.to_thread(_work)

    async def set_state(self, user_id: str, charges: int, last_tick: datetime):
        def _work():
            with self.psycopg.connect(self.dsn) as con:
                con.execute("""
                INSERT INTO stamina(user_id, charges, last_tick_utc)
                VALUES(%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE
                SET charges=EXCLUDED.charges,
                    last_tick_utc=EXCLUDED.last_tick_utc
                """, (user_id, int(charges), last_tick))
                con.commit()
        await asyncio.to_thread(_work)

storage = PostgresStorage(DATABASE_URL) if DATABASE_URL else None
if storage is None:
    raise RuntimeError("DATABASE_URL が未設定です（render.yamlでDB作る前提の構成です）")

# ===== Discord UI =====
class StaminaPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _load_and_recover(self, user_id: str):
        charges, last_tick = await storage.ensure_user(user_id)
        now = utcnow()
        charges, last_tick = calc_recovered(charges, last_tick, now)
        await storage.set_state(user_id, charges, last_tick)
        return charges, last_tick, now

    @discord.ui.button(label="表示/更新", style=discord.ButtonStyle.secondary, custom_id="stamina:show")
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        charges, last_tick, now = await self._load_and_recover(str(interaction.user.id))
        await interaction.response.send_message(
            render_status(interaction.user, charges, last_tick, now),
            ephemeral=True
        )

    @discord.ui.button(label="使用する（0にリセット）", style=discord.ButtonStyle.primary, custom_id="stamina:use")
async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
    user_id = str(interaction.user.id)

    # 最新の回復を反映してから判定
    charges, last_tick, _ = await self._load_and_recover(user_id)

    if charges <= 0:
        return await interaction.response.send_message("❌ 回復回数がありません（0/5）", ephemeral=True)

    # 押したら必ず0にする＆タイマーも今から再スタート
    now = utcnow()
    await storage.set_state(user_id, 0, now)

    await interaction.response.send_message(
        f"✅ 使用しました。**{charges} 回分**を消費して **0/5** にリセットしました。\n"
        f"⏱ 次の+1は **3時間後** から始まります。",
        ephemeral=True
    )

    @discord.ui.button(label="0にリセット", style=discord.ButtonStyle.danger, custom_id="stamina:reset")
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        now = utcnow()
        await storage.set_state(user_id, 0, now)  # 0＆タイマーを今から
        await interaction.response.send_message("✅ 0にリセットしました（回復は今から3時間ごと）", ephemeral=True)

# ===== Bot本体 =====
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="stamina_setup", description="スタミナ管理パネル（ボタン）を設置（管理者）")
@app_commands.checks.has_permissions(administrator=True)
async def stamina_setup(interaction: discord.Interaction):
    await interaction.response.send_message(
        "⚡ **スタミナ管理パネル**\n（ボタンで操作してください）",
        view=StaminaPanel()
    )

# ===== HTTP（Render無料Web用：Pingされる口）=====
async def start_web_server():
    app = web.Application()
    async def health(_request):
        return web.Response(text="ok")
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))  # RenderがPORTを渡す
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

@client.event
async def on_ready():
    await storage.init()

    # 再起動後もボタンが生きるように永続View登録
    client.add_view(StaminaPanel())

    await tree.sync()

    # Webサービスとして落ちないよう、HTTPも起動
    await start_web_server()

    print(f"Logged in as {client.user} ✅")

client.run(TOKEN)
