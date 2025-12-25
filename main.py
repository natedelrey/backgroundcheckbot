import os
import re
import requests
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

import asyncpg

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROVER_API_KEY = os.getenv("ROVER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ROVER_BASE = "https://registry.rover.link/api"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

db_pool: asyncpg.Pool | None = None

# =====================================================
# DB init + helpers
# =====================================================

CREATE_TABLES_SQL = """
create table if not exists watched_groups (
  guild_id bigint not null,
  group_id bigint not null,
  label text,
  primary key (guild_id, group_id)
);

create table if not exists blacklisted_ranks (
  guild_id bigint not null,
  group_id bigint not null,
  rank_id int not null,
  reason text,
  added_by bigint,
  added_at timestamptz not null default now(),
  primary key (guild_id, group_id, rank_id)
);

create table if not exists blacklisted_users (
  guild_id bigint not null,
  roblox_user_id bigint not null,
  reason text not null,
  added_by bigint,
  added_at timestamptz not null default now(),
  primary key (guild_id, roblox_user_id)
);

create table if not exists ranklocks (
  guild_id bigint not null,
  roblox_user_id bigint not null,
  group_id bigint not null,
  max_rank_id int not null,
  reason text not null,
  set_by bigint,
  set_at timestamptz not null default now(),
  primary key (guild_id, roblox_user_id, group_id)
);
"""

async def db_exec(sql: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.execute(sql, *args)

async def db_fetch(sql: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetch(sql, *args)

async def db_fetchrow(sql: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetchrow(sql, *args)

# =====================================================
# Roblox / RoVer helpers
# =====================================================

def discord_to_roblox(guild_id: int, discord_id: int) -> int | None:
    headers = {"Authorization": f"Bearer {ROVER_API_KEY}"}
    url = f"{ROVER_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    rid = data.get("robloxId") or data.get("roblox_id") or data.get("id")
    return int(rid) if rid else None

def username_to_roblox(username: str) -> int | None:
    r = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
        timeout=15
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return int(data["data"][0]["id"])

def get_roblox_user(user_id: int) -> dict:
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=15)
    r.raise_for_status()
    return r.json()

def get_user_groups(user_id: int) -> list:
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles", timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])

def account_age_days(created_iso: str) -> int:
    created_dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days

def safe_text(s: str, max_len: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")

def fmt_date(dt: datetime | None) -> str:
    if not dt:
        return "unknown"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

def chunk_lines(lines: list[str], max_chars: int = 1024) -> list[str]:
    chunks = []
    cur = ""
    for line in lines:
        # +1 newline
        if len(cur) + len(line) + 1 > max_chars:
            if cur.strip():
                chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks

# =====================================================
# Config commands
# =====================================================

@tree.command(name="watchgroup", description="Manage watched Roblox groups (shown in /bgcheck)")
@app_commands.describe(action="add/remove/list", group_id="Roblox group id", label="Optional label shown in embeds")
async def watchgroup(interaction: discord.Interaction, action: str, group_id: str | None = None, label: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)

    if action == "add":
        if not group_id:
            return await interaction.response.send_message("Usage: `/watchgroup action:add group_id:<id> label:<optional>`", ephemeral=True)
        await db_exec(
            "insert into watched_groups (guild_id, group_id, label) values ($1,$2,$3) on conflict (guild_id, group_id) do update set label=excluded.label",
            gid, int(group_id), label
        )
        return await interaction.response.send_message("✅ Watched group added/updated.", ephemeral=True)

    if action == "remove":
        if not group_id:
            return await interaction.response.send_message("Usage: `/watchgroup action:remove group_id:<id>`", ephemeral=True)
        await db_exec("delete from watched_groups where guild_id=$1 and group_id=$2", gid, int(group_id))
        return await interaction.response.send_message("✅ Watched group removed.", ephemeral=True)

    if action == "list":
        rows = await db_fetch("select group_id, label from watched_groups where guild_id=$1 order by group_id asc", gid)
        if not rows:
            return await interaction.response.send_message("No watched groups set.", ephemeral=True)
        lines = [f"• `{r['group_id']}` — {r['label'] or 'no label'}" for r in rows]
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / list", ephemeral=True)


@tree.command(name="blacklistrank", description="Flag specific group rank IDs as blacklisted")
@app_commands.describe(action="add/remove/list", group_id="Roblox group id", rank_id="Roblox rank id (0-255)", reason="Optional reason")
async def blacklistrank(interaction: discord.Interaction, action: str, group_id: str | None = None, rank_id: int | None = None, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)

    if action == "add":
        if not group_id or rank_id is None:
            return await interaction.response.send_message("Usage: `/blacklistrank action:add group_id:<id> rank_id:<num> reason:<optional>`", ephemeral=True)
        await db_exec(
            """insert into blacklisted_ranks (guild_id, group_id, rank_id, reason, added_by)
               values ($1,$2,$3,$4,$5)
               on conflict (guild_id, group_id, rank_id) do update set reason=excluded.reason, added_by=excluded.added_by, added_at=now()""",
            gid, int(group_id), int(rank_id), reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ Blacklisted rank rule added/updated.", ephemeral=True)

    if action == "remove":
        if not group_id or rank_id is None:
            return await interaction.response.send_message("Usage: `/blacklistrank action:remove group_id:<id> rank_id:<num>`", ephemeral=True)
        await db_exec("delete from blacklisted_ranks where guild_id=$1 and group_id=$2 and rank_id=$3", gid, int(group_id), int(rank_id))
        return await interaction.response.send_message("✅ Blacklisted rank removed.", ephemeral=True)

    if action == "list":
        rows = await db_fetch("select group_id, rank_id, reason from blacklisted_ranks where guild_id=$1 order by group_id asc, rank_id asc", gid)
        if not rows:
            return await interaction.response.send_message("No blacklisted ranks set.", ephemeral=True)
        lines = [f"• `{r['group_id']}` rank **{r['rank_id']}** — {r['reason'] or 'no reason'}" for r in rows]
        chunks = chunk_lines(lines, 1800)
        # send first chunk (ephemeral message limit is bigger than embed limits)
        return await interaction.response.send_message(chunks[0], ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / list", ephemeral=True)


@tree.command(name="blacklistuser", description="Blacklist a Roblox userId (hard flag in /bgcheck)")
@app_commands.describe(action="add/remove/check", roblox_id="Roblox user id", reason="Reason for blacklist")
async def blacklistuser(interaction: discord.Interaction, action: str, roblox_id: str, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)
    rid = int(roblox_id)

    if action == "add":
        if not reason:
            return await interaction.response.send_message("Usage: `/blacklistuser action:add roblox_id:<id> reason:<text>`", ephemeral=True)
        await db_exec(
            """insert into blacklisted_users (guild_id, roblox_user_id, reason, added_by)
               values ($1,$2,$3,$4)
               on conflict (guild_id, roblox_user_id) do update set reason=excluded.reason, added_by=excluded.added_by, added_at=now()""",
            gid, rid, reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ User blacklisted.", ephemeral=True)

    if action == "remove":
        await db_exec("delete from blacklisted_users where guild_id=$1 and roblox_user_id=$2", gid, rid)
        return await interaction.response.send_message("✅ User removed from blacklist (if they were on it).", ephemeral=True)

    if action == "check":
        row = await db_fetchrow("select reason, added_at from blacklisted_users where guild_id=$1 and roblox_user_id=$2", gid, rid)
        if not row:
            return await interaction.response.send_message("Not blacklisted.", ephemeral=True)
        return await interaction.response.send_message(f"🚫 Blacklisted — {row['reason']} (since {fmt_date(row['added_at'])})", ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / check", ephemeral=True)

# =====================================================
# Ranklock (DB)
# =====================================================

@tree.command(name="ranklock", description="Set/view/remove a max rank cap for a Roblox user in a group")
@app_commands.describe(
    action="set/remove/view",
    roblox_id="Roblox user ID",
    group_id="Roblox group ID (needed for set/remove)",
    max_rank_id="Max allowed rank id (needed for set)",
    reason="Reason (needed for set)"
)
async def ranklock(interaction: discord.Interaction, action: str, roblox_id: str, group_id: str | None = None, max_rank_id: int | None = None, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Roles** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    guild_id = int(interaction.guild_id)
    rid = int(roblox_id)

    if action == "set":
        if not group_id or max_rank_id is None or not reason:
            return await interaction.response.send_message("Usage: `/ranklock action:set roblox_id:<id> group_id:<id> max_rank_id:<num> reason:<text>`", ephemeral=True)

        await db_exec(
            """insert into ranklocks (guild_id, roblox_user_id, group_id, max_rank_id, reason, set_by)
               values ($1,$2,$3,$4,$5,$6)
               on conflict (guild_id, roblox_user_id, group_id)
               do update set max_rank_id=excluded.max_rank_id, reason=excluded.reason, set_by=excluded.set_by, set_at=now()""",
            guild_id, rid, int(group_id), int(max_rank_id), reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ Ranklock set/updated.", ephemeral=True)

    if action == "remove":
        if not group_id:
            return await interaction.response.send_message("Usage: `/ranklock action:remove roblox_id:<id> group_id:<id>`", ephemeral=True)
        await db_exec("delete from ranklocks where guild_id=$1 and roblox_user_id=$2 and group_id=$3", guild_id, rid, int(group_id))
        return await interaction.response.send_message("✅ Ranklock removed (if it existed).", ephemeral=True)

    if action == "view":
        rows = await db_fetch(
            "select group_id, max_rank_id, reason, set_at from ranklocks where guild_id=$1 and roblox_user_id=$2 order by group_id asc",
            guild_id, rid
        )
        if not rows:
            return await interaction.response.send_message("No ranklocks found.", ephemeral=True)
        lines = [f"• `{r['group_id']}` max **{r['max_rank_id']}** (set {fmt_date(r['set_at'])}) — {r['reason']}" for r in rows]
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    return await interaction.response.send_message("Actions: set / remove / view", ephemeral=True)

# =====================================================
# /bgcheck (pretty + watched groups + flags)
# =====================================================

@tree.command(name="bgcheck", description="Background check a Roblox account (clean + flagged results)")
@app_commands.describe(
    discord_user="Discord user (uses RoVer verification)",
    roblox_id="Roblox userId",
    username="Roblox username",
    show_all="Show every group (normally it shows watched + flagged only)"
)
async def bgcheck(
    interaction: discord.Interaction,
    discord_user: discord.Member | None = None,
    roblox_id: str | None = None,
    username: str | None = None,
    show_all: bool = False
):
    await interaction.response.defer(ephemeral=True)

    # --- resolve roblox userId ---
    target_roblox_id: int | None = None
    if discord_user:
        target_roblox_id = discord_to_roblox(int(interaction.guild_id), int(discord_user.id))
        if not target_roblox_id:
            return await interaction.followup.send("❌ That Discord user is not verified with RoVer.")
    elif roblox_id:
        target_roblox_id = int(roblox_id)
    elif username:
        target_roblox_id = username_to_roblox(username)
        if not target_roblox_id:
            return await interaction.followup.send("❌ Roblox username not found.")
    else:
        return await interaction.followup.send("❌ Provide discord_user OR roblox_id OR username.")

    # --- fetch roblox data ---
    user = get_roblox_user(target_roblox_id)
    groups = get_user_groups(target_roblox_id)

    # sort groups by name
    groups_sorted = sorted(groups, key=lambda x: (x["group"]["name"] or "").lower())

    # --- load config/rules ---
    guild_id = int(interaction.guild_id)

    watched = await db_fetch("select group_id, label from watched_groups where guild_id=$1", guild_id)
    watched_map = {int(r["group_id"]): (r["label"] or None) for r in watched}

    blacklisted_rank_rows = await db_fetch("select group_id, rank_id, reason from blacklisted_ranks where guild_id=$1", guild_id)
    blrank_map: dict[tuple[int, int], str | None] = {(int(r["group_id"]), int(r["rank_id"])): (r["reason"] or None) for r in blacklisted_rank_rows}

    user_blacklist = await db_fetchrow("select reason, added_at from blacklisted_users where guild_id=$1 and roblox_user_id=$2", guild_id, target_roblox_id)

    ranklocks = await db_fetch(
        "select group_id, max_rank_id, reason, set_at from ranklocks where guild_id=$1 and roblox_user_id=$2",
        guild_id, target_roblox_id
    )
    ranklock_map = {int(r["group_id"]): r for r in ranklocks}

    # --- compute trust + flags ---
    age_days = account_age_days(user["created"])
    created_date = user["created"][:10]

    trust_notes = []
    if age_days < 7:
        trust_notes.append("Very new account (<7 days)")
    elif age_days < 30:
        trust_notes.append("New-ish account (<30 days)")

    if user_blacklist:
        trust_notes.append(f"🚫 Blacklisted user: {user_blacklist['reason']} (since {fmt_date(user_blacklist['added_at'])})")

    # --- build group lines (watched + flagged; or all) ---
    lines = []
    flagged_count = 0
    watched_count = 0

    for g in groups_sorted:
        gid = int(g["group"]["id"])
        gname = g["group"]["name"]
        rank_id = int(g["role"]["rank"])
        role_name = g["role"]["name"]

        is_watched = gid in watched_map
        if is_watched:
            watched_count += 1

        bl_reason = blrank_map.get((gid, rank_id))
        is_blacklisted_rank = bl_reason is not None

        rl = ranklock_map.get(gid)
        rl_txt = ""
        rl_flag = False
        if rl:
            max_rank = int(rl["max_rank_id"])
            rl_flag = rank_id > max_rank
            rl_txt = f" | RL max **{max_rank}** ({'⚠️ exceeds' if rl_flag else 'ok'}) set {fmt_date(rl['set_at'])}"

        # Decide if we show it
        should_show = show_all or is_watched or is_blacklisted_rank or rl_flag
        if not should_show:
            continue

        icon = "✅"
        extra = ""
        label = watched_map.get(gid)
        display_name = label or gname

        if is_blacklisted_rank:
            icon = "🚫"
            flagged_count += 1
            extra = f" — **BLACKLIST** ({safe_text(bl_reason or 'rule hit', 80)})"
        elif rl_flag:
            icon = "⚠️"
            flagged_count += 1
            extra = " — **RANKLOCK EXCEEDED**"

        line = f"{icon} **{safe_text(display_name, 42)}** (`{gid}`) — **{rank_id}** ({safe_text(role_name, 40)}){extra}{rl_txt}"
        lines.append(line)

    # fallback if nothing to show
    if not lines and not show_all:
        lines = ["No watched/flagged groups matched. (Use `show_all:true` to display every group.)"]

    # chunk lines into embed fields
    chunks = chunk_lines(lines, 1024)

    # --- embed layout ---
    title_name = f"{user['name']} ({user['displayName']})"
    embed = discord.Embed(
        title="Roblox Background Check",
        description=f"**{safe_text(title_name, 80)}**\nID: `{target_roblox_id}`",
        color=0x2f3136
    )

    embed.add_field(
        name="Account",
        value=f"Created: **{created_date}**\nAge: **{age_days} days**",
        inline=True
    )

    embed.add_field(
        name="Summary",
        value=f"Watched in: **{watched_count}**\nFlags: **{flagged_count}**\nTotal groups: **{len(groups_sorted)}**",
        inline=True
    )

    if trust_notes:
        embed.add_field(name="Notes", value="\n".join([f"• {safe_text(n, 180)}" for n in trust_notes])[:1024], inline=False)

    # Add group fields (keep under Discord 25-field cap)
    # We already used 2 fields, + maybe Notes. So allow up to 20 chunks.
    max_group_fields = 20
    for i, chunk in enumerate(chunks[:max_group_fields]):
        embed.add_field(
            name="Groups" if i == 0 else f"Groups (cont. {i})",
            value=chunk,
            inline=False
        )

    embed.set_footer(text=f"Checked by {interaction.user}")

    await interaction.followup.send(embed=embed)

# =====================================================
# Startup
# =====================================================

@client.event
async def on_ready():
    global db_pool
    if db_pool is None:
        if not DATABASE_URL:
            print("❌ DATABASE_URL missing. Add Railway Postgres and set DATABASE_URL.")
            return
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await db_exec(CREATE_TABLES_SQL)
        print("✅ DB ready (tables ensured).")

    await tree.sync()
    print(f"✅ Logged in as {client.user}")

client.run(DISCORD_TOKEN)
