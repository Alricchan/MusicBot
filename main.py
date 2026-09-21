import asyncio
import os
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

IDLE_SECONDS = 300  # leave the voice channel after 5 minutes without audio

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",  # plain text gets searched on YouTube
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",  # drop any video stream
}

# Reaction controls. Each emoji includes the \ufe0f variation selector so Discord
# accepts it when the bot adds it; incoming reactions are compared without it.
CONTROLS = {
    "\u23ee\ufe0f": "restart",  # ⏮️
    "\u23ef\ufe0f": "toggle",   # ⏯️
    "\u23ed\ufe0f": "skip",     # ⏭️
    "\u23f9\ufe0f": "stop",     # ⏹️
    "\ud83d\udccb": "queue",    # 📋 (New list control!)
}
ACTIONS = {emoji.replace("\ufe0f", ""): action for emoji, action in CONTROLS.items()}

# Per-guild state, keyed by guild id
queues: dict[int, deque] = {}                          # upcoming tracks
current: dict[int, dict] = {}                          # track that is (or was last) playing
idle_tasks: dict[int, asyncio.Task] = {}               # pending auto-disconnect timers
panels: dict[int, discord.Message] = {}                # the "now playing" message with reactions
text_channels: dict[int, discord.abc.Messageable] = {} # where to post that message
queue_messages: dict[int, discord.Message] = {}        # Tracks the printed queue display message

background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks from being garbage collected


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


class BuddyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = BuddyBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


# ---------- idle timer ----------

def cancel_idle(guild_id: int) -> None:
    task = idle_tasks.pop(guild_id, None)
    if task:
        task.cancel()


def start_idle_timer(guild: discord.Guild) -> None:
    cancel_idle(guild.id)
    idle_tasks[guild.id] = asyncio.create_task(idle_disconnect(guild))


async def idle_disconnect(guild: discord.Guild) -> None:
    await asyncio.sleep(IDLE_SECONDS)
    idle_tasks.pop(guild.id, None)  # remove ourselves first so stop_all doesn't cancel this task
    vc = guild.voice_client
    if vc and not vc.is_playing():  # paused counts as idle
        await stop_all(guild)


# ---------- playback ----------

def extract(query: str) -> dict:
    """Blocking: resolve a URL or search string to a playable stream."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    if "entries" in info:  # a search returns a list of results
        entries = list(info["entries"])
        if not entries:
            raise ValueError("no results found")
        info = entries[0]
    return {"title": info["title"], "stream_url": info["url"]}


def play_next(guild: discord.Guild, loop: asyncio.AbstractEventLoop) -> None:
    """Start the next queued track, or start the idle timer if there is none."""
    vc = guild.voice_client
    if vc is None:
        cancel_idle(guild.id)
        return

    queue = queues.get(guild.id)
    if not queue:
        start_idle_timer(guild)
        return

    cancel_idle(guild.id)
    track = queue.popleft()
    current[guild.id] = track
    source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTS)

    def after(error):
        # Runs in the audio thread, so hop back onto the event loop.
        if error:
            print(f"Playback error: {error}")
        loop.call_soon_threadsafe(play_next, guild, loop)

    vc.play(source, after=after)
    spawn(post_panel(guild, track["title"]))
    spawn(clear_queue_message(guild.id))  # Delete queue text when a new song rolls over


async def stop_all(guild: discord.Guild) -> None:
    queues.pop(guild.id, None)
    current.pop(guild.id, None)
    cancel_idle(guild.id)
    await clear_panel(guild.id)
    await clear_queue_message(guild.id)
    vc = guild.voice_client
    if vc:
        await vc.disconnect()
    cancel_idle(guild.id)  # the audio thread's `after` may have started a timer meanwhile


# ---------- reaction panel ----------

async def clear_panel(guild_id: int) -> None:
    panel = panels.pop(guild_id, None)
    if panel:
        try:
            await panel.delete()
        except discord.HTTPException:
            pass


async def clear_queue_message(guild_id: int) -> None:
    """Helper to delete an existing queue lookup layout message safely."""
    q_msg = queue_messages.pop(guild_id, None)
    if q_msg:
        try:
            await q_msg.delete()
        except discord.HTTPException:
            pass


async def post_panel(guild: discord.Guild, title: str) -> None:
    await clear_panel(guild.id)
    channel = text_channels.get(guild.id)
    if channel is None:
        return
    try:
        msg = await channel.send(
            f"Now playing: **{title}**\n"
            "⏮️ restart  ·  ⏯️ pause/resume  ·  ⏭️ skip  ·  ⏹️ stop  ·  📋 show queue"
        )
        panels[guild.id] = msg
        for emoji in CONTROLS:
            await msg.add_reaction(emoji)
    except discord.HTTPException as e:
        print(f"Couldn't post the controls: {e}")


async def handle_reaction(payload: discord.RawReactionActionEvent) -> None:
    if payload.user_id == bot.user.id or payload.guild_id is None:
        return

    panel = panels.get(payload.guild_id)
    if panel is None or panel.id != payload.message_id:
        return  # not the current "now playing" message

    action = ACTIONS.get((payload.emoji.name or "").replace("\ufe0f", ""))
    guild = bot.get_guild(payload.guild_id)
    if action is None or guild is None:
        return

    # Only people sitting in the bot's voice channel may control it.
    vc = guild.voice_client
    member = guild.get_member(payload.user_id)
    if vc is None or member is None or member.voice is None or member.voice.channel != vc.channel:
        return

    loop = asyncio.get_running_loop()
    active = vc.is_playing() or vc.is_paused()

    if action == "toggle":
        if vc.is_playing():
            vc.pause()
            start_idle_timer(guild)
        elif vc.is_paused():
            vc.resume()
            cancel_idle(guild.id)
    elif action == "skip":
        if active:
            vc.stop()  # triggers `after`, which starts the next track
    elif action == "restart":
        track = current.get(guild.id)
        if track is None:
            return
        queues.setdefault(guild.id, deque()).appendleft(track)
        if active:
            vc.stop()  # `after` then plays the same track again from the start
        else:
            play_next(guild, loop)  # the track had already finished
    elif action == "stop":
        await stop_all(guild)
    elif action == "queue":
        # First, remove old queue if it exists
        await clear_queue_message(guild.id)
        
        # Pull text channel target
        channel = text_channels.get(guild.id)
        if not channel:
            return

        # Compile queue output
        queue = queues.get(guild.id)
        if not queue:
            msg = await channel.send("The queue is empty.")
            queue_messages[guild.id] = msg
            return

        # Render list tracking
        lines = [f"{i}. {t['title']}" for i, t in enumerate(list(queue)[:10], start=1)]
        extra = f"\n...and {len(queue) - 10} more" if len(queue) > 10 else ""
        
        msg = await channel.send(f"**Upcoming Songs:**\n" + "\n".join(lines) + extra)
        queue_messages[guild.id] = msg


# Each click either adds or removes the user's reaction, so listen for both.
# That way the controls work repeatedly without the bot needing Manage Messages.
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await handle_reaction(payload)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await handle_reaction(payload)


# ---------- slash commands ----------

@bot.tree.command(name="ping", description="Check that the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Meow >.<")


@bot.tree.command(name="play", description="Play a song from a URL or search terms")
@app_commands.describe(query="YouTube URL or search terms")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    if interaction.user.voice is None:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer()  # extraction can take several seconds

    try:
        track = await asyncio.to_thread(extract, query)
    except Exception as e:
        await interaction.followup.send(f"Couldn't fetch that: {e}")
        return

    guild = interaction.guild
    channel = interaction.user.voice.channel
    vc = guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    text_channels[guild.id] = interaction.channel  # where the reaction panel gets posted
    queues.setdefault(guild.id, deque()).append(track)

    # Delete old stale queue message since the queue just changed/updated!
    await clear_queue_message(guild.id)

    if not (vc.is_playing() or vc.is_paused()):
        play_next(guild, asyncio.get_running_loop())

    await interaction.followup.send(f"Added **{track['title']}** to the queue.")


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # triggers `after`, which starts the next track
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
 
 
@bot.tree.command(name="queue", description="Show upcoming songs")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    queue = queues.get(interaction.guild.id)
    if not queue:
        await interaction.response.send_message("The queue is empty.")
        return
    lines = [f"{i}. {t['title']}" for i, t in enumerate(list(queue)[:10], start=1)]
    extra = f"\n...and {len(queue) - 10} more" if len(queue) > 10 else ""
    await interaction.response.send_message("\n".join(lines) + extra)
 
 
@bot.tree.command(name="stop", description="Stop playing, clear the queue, and leave")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await stop_all(interaction.guild)
    await interaction.response.send_message("Stopped.")
 
 
bot.run(os.environ["DISCORD_TOKEN"])