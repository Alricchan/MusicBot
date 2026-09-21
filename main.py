import os
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

class BuddyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()  # registers slash commands with Discord

bot = BuddyBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.tree.command(name="ping", description="Check that the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Meow >.<")

bot.run(os.environ["DISCORD_TOKEN"])