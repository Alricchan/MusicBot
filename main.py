import asyncio
import os
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

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

# guild id -> upcoming tracks (each guild gets its own queue)
queues: dict[int, deque] = {}


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
    """Pop the next track and start playing it. Does nothing if the queue is empty."""
    vc = guild.voice_client
    queue = queues.get(guild.id)
    if vc is None or not queue:
        return

    track = queue.popleft()
    source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTS)

    def after(error):
        # Runs in a separate audio thread, so hop back onto the event loop.
        if error:
            print(f"Playback error: {error}")
        loop.call_soon_threadsafe(play_next, guild, loop)

    vc.play(source, after=after)


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

    queues.setdefault(guild.id, deque()).append(track)

    if vc.is_playing() or vc.is_paused():
        await interaction.followup.send(f"Queued: **{track['title']}**")
    else:
        play_next(guild, asyncio.get_running_loop())
        await interaction.followup.send(f"Now playing: **{track['title']}**")


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
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
    queues.pop(interaction.guild.id, None)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
    await interaction.response.send_message("Stopped.")


bot.run(os.environ["DISCORD_TOKEN"])