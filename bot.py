import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import json
import os

# 🔥 Railway用（環境変数から取得）
TOKEN = os.getenv("TOKEN")

if TOKEN is None:
    print("TOKENが設定されていません。RailwayのVariablesを確認してください。")
    exit()

TARGET_THREADS = [
    1473961871806824562,
    1473961111253422181,
    1473955000626712628,
    1473948518371557429,
    1473945777339633724,
    1473941857405767926,
    1473937540502392958,
    1473935162546061323,
    1473929826409906267,
    1473922879430066176,
    1473919525308338238,
    1473909245853306930,
    1473981529288867911,
    1473980498840784987,
    1473978315797696512,
    1473974650768986193,
    1473973872062889984,
    1473973128525906044,
    1473965496775737476,
    1473962738526191719
]

MAX_COUNT = 5
RECOVERY_HOURS = 3
DATA_FILE = "data.json"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# データ読み込み
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
else:
    data = {}

active_messages = {}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def get_thread_data(thread_id):
    if str(thread_id) not in data:
        data[str(thread_id)] = {
            "count": MAX_COUNT,
            "last_time": datetime.utcnow().isoformat()
        }
    return data[str(thread_id)]

def recover(thread_id):
    thread_data = get_thread_data(thread_id)
    now = datetime.utcnow()
    last_time = datetime.fromisoformat(thread_data["last_time"])
    diff = now - last_time
    recovery = int(diff.total_seconds() // (RECOVERY_HOURS * 3600))

    if recovery > 0:
        thread_data["count"] = min(MAX_COUNT, thread_data["count"] + recovery)
        thread_data["last_time"] = now.isoformat()

def get_remaining(thread_id):
    thread_data = get_thread_data(thread_id)

    if thread_data["count"] >= MAX_COUNT:
        return None

    last_time = datetime.fromisoformat(thread_data["last_time"])
    next_time = last_time + timedelta(hours=RECOVERY_HOURS)
    remain = next_time - datetime.utcnow()

    seconds = int(remain.total_seconds())
    if seconds <= 0:
        return None

    h, r = divmod(seconds, 3600)
    m, _ = divmod(r, 60)

    return f"{h}時間 {m}分"

class StaminaView(discord.ui.View):
    def __init__(self, thread_id):
        super().__init__(timeout=None)
        self.thread_id = thread_id

    @discord.ui.button(label="使用する", style=discord.ButtonStyle.red)
    async def use_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        recover(self.thread_id)
        thread_data = get_thread_data(self.thread_id)

        if thread_data["count"] > 0:
            thread_data["count"] = 0
            thread_data["last_time"] = datetime.utcnow().isoformat()
            save_data()

            button.disabled = True

            await interaction.response.edit_message(
                content="🔥 使用しました！現在 0/5",
                view=self
            )
        else:
            await interaction.response.send_message("回復待ちです。", ephemeral=True)

@tasks.loop(minutes=1)
async def auto_update():
    for thread_id in TARGET_THREADS:
        recover(thread_id)
        save_data()

        try:
            thread = await bot.fetch_channel(thread_id)
            message = await thread.fetch_message(active_messages[thread_id])
        except:
            continue

        thread_data = get_thread_data(thread_id)
        count = thread_data["count"]
        remaining = get_remaining(thread_id)

        if remaining:
            text = f"現在の残り回数: {count}/5\n次回復まで: {remaining}"
        else:
            text = f"現在の残り回数: {count}/5\n✨ フル回復しています"

        view = StaminaView(thread_id)

        if count == 0:
            for item in view.children:
                item.disabled = True

        await message.edit(content=text, view=view)

@bot.event
async def on_ready():
    print("Bot起動完了")

    for thread_id in TARGET_THREADS:
        try:
            thread = await bot.fetch_channel(thread_id)
        except:
            continue

        recover(thread_id)
        save_data()

        thread_data = get_thread_data(thread_id)
        count = thread_data["count"]
        remaining = get_remaining(thread_id)

        if remaining:
            text = f"現在の残り回数: {count}/5\n次回復まで: {remaining}"
        else:
            text = f"現在の残り回数: {count}/5\n✨ フル回復しています"

        view = StaminaView(thread_id)

        message = await thread.send(text, view=view)
        active_messages[thread_id] = message.id

    auto_update.start()

bot.run(TOKEN)
