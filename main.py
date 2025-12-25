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

# =====================================================
# Ranklock storage (JSON)
# =====================================================

def _load_ranklocks():
    if not os.path.exists(RANKLOCK_FILE):
        return {}
    try:
        with open(RANKLOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ranklocks(data):
    with open(RANKLOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_ranklock(guild_id, roblox_id, group_id):
    data = _load_ranklocks()
    return data.get(str(guild_id), {}).get(str(roblox_id), {}).get(str(group_id))

def set_ranklock(guild_id, roblox_id, group_id, max_rank_id, reason, set_by):
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

def remove_ranklock(guild_id, roblox_id, group_id):
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

def list_ranklocks(guild_id, roblox_id):
    data = _load_ranklocks()
    return data.get(str(guild_id), {}).get(str(roblox_id), {})

# =====================================================
# Roblox / RoVer helpers
# =====================================================

def discord_to_roblox(guild_id, discord_id):
    headers = {"Authorization": f"Bearer {ROVER_API_KEY}"}
    url = f"{ROVER_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return None
    data = r.json()
    rid = data.get("robloxId") or data.get("roblox_id") or data.get("id")
    return int(rid) if rid else None

def username_to_roblox(username):
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

def get_roblox_user(user_id):
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
    r.raise_for_status()
    return r.json()

def get_user_groups(user_id):
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
    r.raise_for_status()
    return r.json().get("data", [])

def account_age_days(created):
    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days

# =====================================================
# /ranklock command
# =====================================================

@tree.command(name="ranklock", description="Manage ranklocks")
@app_commands.describe(
    action="set / remove / view",
    roblox_id="Roblox user ID",
    group_id="Roblox group ID",
    max_rank_id="Max allowed rank ID",
    reason="Reason for ranklock"
)
async def ranklock(
    interaction: discord.Interaction,
    action: str,
    roblox_id: str,
    group_id: str | None = None,
    max_rank_id: int | None = None,
    reason: str | None = None
):
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Roles** or **Admin**.", ephemeral=True)

    action = action.lower()
    rid = int(roblox_id)
    gid = interaction.guild_id

    if action == "set":
        if not group_id or max_rank_id is None or not reason:
            return await interaction.response.send_message("Missing fields.", ephemeral=True)

        set_ranklock(gid, rid, int(group_id), max_rank_id, reason, interaction.user.id)
        return await interaction.response.send_message("✅ Ranklock set.", ephemeral=True)

    if action == "remove":
        if not group_id:
            return await interaction.response.send_message("Missing group_id.", ephemeral=True)

        ok = remove_ranklock(gid, rid, int(group_id))
        return await interaction.response.send_message("✅ Removed." if ok else "No ranklock found.", ephemeral=True)

    if action == "view":
        locks = list_ranklocks(gid, rid)
        if not locks:
            return await interaction.response.send_message("No ranklocks found.", ephemeral=True)

        lines = []
        for group_id, info in locks.items():
            dt = info["set_at"][:10]
            lines.append(f"• Group {group_id} → max {info['max_rank_id']} (set {dt}) — {info['reason']}")

        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    await interaction.response.send_message("Actions: set, remove, view", ephemeral=True)

# =====================================================
# /bgcheck command
# =====================================================

@tree.command(name="bgcheck", description="Background check a Roblox account")
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
            return await interaction.followup.send("❌ User not verified with RoVer.")

    elif roblox_id:
        user_id = int(roblox_id)

    elif username:
        user_id = username_to_roblox(username)
        if not user_id:
            return await interaction.followup.send("❌ Username not found.")

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
        value=f"{age_days} days\nCreated: {user['created'][:10]}",
        inline=False
    )

    if age_days < 30:
        embed.add_field(name="⚠️ Trust Note", value="Account is very new (<30 days).", inline=False)

    # ---------- Group chunking ----------
    lines = []
    for g in groups:
        gid = int(g["group"]["id"])
        gname = g["group"]["name"]
        rank_id = int(g["role"]["rank"])
        role = g["role"]["name"]

        lock = get_ranklock(interaction.guild_id, user_id, gid)
        lock_txt = ""
        if lock:
            max_rank = lock["max_rank_id"]
            exceeds = rank_id > max_rank
            lock_txt = f" | RL max {max_rank} ({'EXCEEDS' if exceeds else 'ok'})"

        line = f"• {gname} ({gid}) — {rank_id} ({role}){lock_txt}"
        if len(line) > 180:
            line = line[:177] + "..."
        lines.append(line)

    if lines:
        chunks = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 1024:
                chunks.append(current.rstrip())
                current = ""
            current += line + "\n"
        if current.strip():
            chunks.append(current.rstrip())

        for i, chunk in enumerate(chunks[:20]):
            embed.add_field(
                name="Groups & Ranks" if i == 0 else f"Groups & Ranks (cont. {i})",
                value=chunk,
                inline=False
            )
    else:
        embed.add_field(name="Groups & Ranks", value="User is not in any groups.", inline=False)

    embed.set_footer(text=f"Checked by {interaction.user}")
    await interaction.followup.send(embed=embed)

# =====================================================
# Startup
# =====================================================

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

client.run(DISCORD_TOKEN)
