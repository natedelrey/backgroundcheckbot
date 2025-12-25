import os
import json
import requests
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROVER_API_KEY = os.getenv("ROVER_API_KEY")
ROVER_BASE = "https://registry.rover.link/api"

RANKLOCK_FILE = "ranklocks.json"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# -------------------------
# Ranklock storage (JSON)
# -------------------------

def _load_ranklocks() -> dict:
    if not os.path.exists(RANKLOCK_FILE):
        return {}
    try:
        with open(RANKLOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ranklocks(data: dict) -> None:
    with open(RANKLOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_ranklock(guild_id: int, roblox_id: int, group_id: int) -> dict | None:
    data = _load_ranklocks()
    return data.get(str(guild_id), {}).get(str(roblox_id), {}).get(str(group_id))

def set_ranklock(guild_id: int, roblox_id: int, group_id: int, max_rank_id: int, reason: str, set_by: int) -> None:
    data = _load_ranklocks()
    g = data.setdefault(str(guild_id), {})
    u = g.setdefault(str(roblox_id), {})
    u[str(group_id)] = {
        "max_rank_id": int(max_rank_id),
        "reason": reason,
        "set_by": str(set_by),
        "set_at": datetime.now(timezone.utc).isoformat()
    }
    _save_ranklocks(data)

def remove_ranklock(guild_id: int, roblox_id: int, group_id: int) -> bool:
    data = _load_ranklocks()
    g = data.get(str(guild_id), {})
    u = g.get(str(roblox_id), {})
    if str(group_id) in u:
        del u[str(group_id)]
        if not u:
            g.pop(str(roblox_id), None)
        _save_ranklocks(data)
        return True
    return False

def list_ranklocks(guild_id: int, roblox_id: int) -> dict:
    data = _load_ranklocks()
    return data.get(str(guild_id), {}).get(str(roblox_id), {})


# -------------------------
# Roblox / RoVer helpers
# -------------------------

def discord_to_roblox(guild_id: int, discord_id: int) -> int | None:
    headers = {"Authorization": f"Bearer {ROVER_API_KEY}"}
    url = f"{ROVER_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return None
    data = r.json()
    rid = data.get("robloxId") or data.get("roblox_id") or data.get("id")
    return int(rid) if rid else None

def username_to_roblox(username: str) -> int | None:
    r = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False}
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return int(data["data"][0]["id"])

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
# Slash: /ranklock
# -------------------------

@tree.command(name="ranklock", description="Manage ranklocks")
@app_commands.describe(
    action="set/remove/view",
    roblox_id="Roblox user ID",
    group_id="Roblox group ID",
    max_rank_id="Max rank ID allowed",
    reason="Why this ranklock exists"
)
async def ranklock(
    interaction: discord.Interaction,
    action: str,
    roblox_id: str,
    group_id: str | None = None,
    max_rank_id: int | None = None,
    reason: str | None = None
):
    # Permission gate (simple)
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Roles** or **Admin** to use ranklock.", ephemeral=True)

    action = action.lower().strip()
    gid = interaction.guild_id
    rid = int(roblox_id)

    if action == "set":
        if group_id is None or max_rank_id is None or reason is None:
            return await interaction.response.send_message("Usage: `/ranklock action:set roblox_id:<id> group_id:<id> max_rank_id:<num> reason:<text>`", ephemeral=True)

        set_ranklock(gid, rid, int(group_id), int(max_rank_id), reason, interaction.user.id)
        return await interaction.response.send_message(
            f"✅ Ranklock set for **{rid}** in group **{group_id}**: max rank **{max_rank_id}**\nReason: {reason}",
            ephemeral=True
        )

    elif action == "remove":
        if group_id is None:
            return await interaction.response.send_message("Usage: `/ranklock action:remove roblox_id:<id> group_id:<id>`", ephemeral=True)

        ok = remove_ranklock(gid, rid, int(group_id))
        return await interaction.response.send_message(
            "✅ Removed." if ok else "No ranklock found for that user/group.",
            ephemeral=True
        )

    elif action == "view":
        locks = list_ranklocks(gid, rid)
        if not locks:
            return await interaction.response.send_message("No ranklocks found.", ephemeral=True)

        lines = []
        for groupId, info in locks.items():
            set_at = info.get("set_at", "")
            try:
                dt = datetime.fromisoformat(set_at.replace("Z", "+00:00"))
                set_at_fmt = dt.strftime("%Y-%m-%d")
            except Exception:
                set_at_fmt = set_at

            lines.append(f"• Group **{groupId}** max **{info['max_rank_id']}** (set {set_at_fmt}) — {info['reason']}")

        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    else:
        return await interaction.response.send_message("Actions: `set`, `remove`, `view`", ephemeral=True)


# -------------------------
# Slash: /bgcheck
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

    embed = discord.Embed(title="Roblox Background Check", color=0x2f3136)
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

    # Simple trust note
    if age_days < 30:
        embed.add_field(name="⚠️ Trust Note", value="Account is very new (<30 days).", inline=False)

    # Groups + ranklocks
    if groups:
        lines = []
        for g in groups:
            group_id = int(g["group"]["id"])
            group_name = g["group"]["name"]
            rank_id = int(g["role"]["rank"])
            role_name = g["role"]["name"]

            lock = get_ranklock(interaction.guild_id, user_id, group_id)
            lock_txt = ""
            if lock:
                # format date
                set_at = lock.get("set_at", "")
                try:
                    dt = datetime.fromisoformat(set_at.replace("Z", "+00:00"))
                    set_fmt = dt.strftime("%Y-%m-%d")
                except Exception:
                    set_fmt = set_at

                max_rank = int(lock["max_rank_id"])
                exceeds = rank_id > max_rank
                lock_txt = f" | Ranklock max **{max_rank}** ({'⚠️ exceeds' if exceeds else 'ok'}) set {set_fmt} — {lock['reason']}"

            lines.append(f"• **{group_name}** (`{group_id}`) — Rank {rank_id} ({role_name}){lock_txt}")

        # Discord embed field limit: keep it reasonable
        embed.add_field(name="Groups & Ranks", value="\n".join(lines[:20]), inline=False)
        if len(lines) > 20:
            embed.add_field(name="More Groups", value=f"...and {len(lines)-20} more not shown.", inline=False)
    else:
        embed.add_field(name="Groups & Ranks", value="User is not in any groups.", inline=False)

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
