import os
import requests
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROVER_API_KEY = os.getenv("ROVER_API_KEY")

ROVER_BASE = "https://registry.rover.link/api"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -------------------------
# Helper functions
# -------------------------

def discord_to_roblox(guild_id: int, discord_id: int) -> int | None:
    headers = {"Authorization": f"Bearer {ROVER_API_KEY}"}
    url = f"{ROVER_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"

    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return None

    data = r.json()
    return int(data.get("robloxId")) if data.get("robloxId") else None


def username_to_roblox(username: str) -> int | None:
    r = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False}
    )
    if r.status_code != 200:
        return None

    data = r.json()
    if not data["data"]:
        return None

    return data["data"][0]["id"]


def get_roblox_user(user_id: int) -> dict:
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    r.raise_for_status()
    return r.json()


def get_user_groups(user_id: int) -> list:
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    r.raise_for_status()
    return r.json().get("data", [])


def account_age_days(created: str) -> int:
    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days


# -------------------------
# Slash command
# -------------------------

@tree.command(name="bgcheck", description="Background check a Roblox account")
@app_commands.describe(
    discord_user="Discord user (uses RoVer verification)",
    roblox_id="Roblox user ID",
    username="Roblox username"
)
async def bgcheck(
    interaction: discord.Interaction,
    discord_user: discord.Member | None = None,
    roblox_id: str | None = None,
    username: str | None = None
):
    await interaction.response.defer(ephemeral=True)

    user_id = None

    if discord_user:
        user_id = discord_to_roblox(interaction.guild_id, discord_user.id)
        if not user_id:
            return await interaction.followup.send("❌ That user is not verified with RoVer.")

    elif roblox_id:
        user_id = int(roblox_id)

    elif username:
        user_id = username_to_roblox(username)
        if not user_id:
            return await interaction.followup.send("❌ Roblox username not found.")

    else:
        return await interaction.followup.send("❌ Provide a Discord user, Roblox ID, or username.")

    user = get_roblox_user(user_id)
    groups = get_user_groups(user_id)

    age_days = account_age_days(user["created"])

    embed = discord.Embed(
        title="Roblox Background Check",
        color=0x2f3136
    )

    embed.add_field(
        name="User",
        value=f"**{user['name']}** ({user['displayName']})\nID: `{user_id}`",
        inline=False
    )

    embed.add_field(
        name="Account Age",
        value=f"{age_days} days old\nCreated: {user['created'][:10]}",
        inline=False
    )

    if age_days < 30:
        embed.add_field(
            name="⚠️ Trust Note",
            value="Account is very new (<30 days)",
            inline=False
        )

    if groups:
        lines = []
        for g in groups:
            lines.append(
                f"• **{g['group']['name']}** — "
                f"Rank {g['role']['rank']} ({g['role']['name']})"
            )

        embed.add_field(
            name="Groups & Ranks",
            value="\n".join(lines[:20]),
            inline=False
        )
    else:
        embed.add_field(
            name="Groups & Ranks",
            value="User is not in any groups.",
            inline=False
        )

    embed.set_footer(text=f"Checked by {interaction.user}")

    await interaction.followup.send(embed=embed)


# -------------------------
# Startup
# -------------------------

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

client.run(DISCORD_TOKEN)
