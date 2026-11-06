"""The slash commands: /zamujeno for everyone, and the /debeloglav admin group.

The /debeloglav group is only for server admins (Manage Server): it is hidden from everyone else
(default_permissions), and every handler re-checks the permission itself. All its replies are
ephemeral. /zamujeno is for everyone and also answers ephemerally, so only the asker sees it.
"""

import logging
import re

import discord
from discord import app_commands

from . import blocked, bot, store
from .agent import memory_prompt, style_prompt
from .context import TIMEZONE, Request
from .mentions import untag, who

log = logging.getLogger("debeloglav")

DISCORD_LIMIT = 2000
ADMIN_ONLY = "Tega lahko uporabljajo samo admini."
ERROR = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek."

# Any channel an admin can block: text, voice, category, forum, stage, or a thread.
Place = discord.abc.GuildChannel | discord.Thread

group = app_commands.Group(
    name="debeloglav",
    description="Nastavitve strežnika (samo za admine)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


async def _admin(interaction: discord.Interaction) -> bool:
    """Re-check Manage Server: default_permissions only hides the command, it does not enforce it."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions is not None and permissions.manage_guild:
        return True
    await interaction.response.send_message(ADMIN_ONLY, ephemeral=True)
    return False


def _scope(interaction: discord.Interaction) -> str:
    return f"guild:{interaction.guild_id}"


def _label(place: Place) -> str:
    return getattr(place, "mention", None) or f"#{place.name}"


def _where(style: store.Style) -> str:
    return (
        ", ".join(f"#{name}" for name in style.channels)
        if style.channels
        else "vsi kanali"
    )


def _clip(text: str) -> str:
    return text if len(text) <= DISCORD_LIMIT else text[: DISCORD_LIMIT - 1] + "…"


@group.command(
    name="nastavitve",
    description="Pokaži blokirane kanale, učenje sloga, spomine in naloge",
)
async def nastavitve(interaction: discord.Interaction) -> None:
    if not await _admin(interaction):
        return
    assert interaction.guild_id is not None
    scope = _scope(interaction)
    env_ids = sorted(blocked.BLOCKED_CHANNELS)
    db_ids = store.blocked_channels(interaction.guild_id)
    current = store.style(scope)
    learned = (
        f"{current.learned_at.astimezone(TIMEZONE):%Y-%m-%d %H:%M}"
        if current.learned_at
        else "še ni naučeno"
    )
    lines = [
        "**Blokirani kanali** (bot jih ne bere):",
        f"- iz .env: {', '.join(f'<#{i}>' for i in env_ids) if env_ids else 'nič'}",
        f"- tega strežnika: {', '.join(f'<#{i}>' for i in db_ids) if db_ids else 'nič'}",
        "",
        "**Učenje sloga:**",
        f"- vklopljeno: {'da' if current.enabled else 'ne'}",
        f"- kanali: {', '.join(f'#{c}' for c in current.channels) if current.channels else 'vsi kanali'}",
        f"- nazadnje naučeno: {learned}",
        "",
        f"**Spomini:** {len(store.memories(scope))}",
        f"**Aktivne naloge:** {len(store.tasks(scope))}",
    ]
    await interaction.response.send_message(_clip("\n".join(lines)), ephemeral=True)


@group.command(
    name="blokiraj", description="Blokiraj kanal ali kategorijo: bot ga ne bo več bral"
)
@app_commands.describe(kanal="Kanal, kategorija, forum ali nit, ki jo blokiraš")
async def blokiraj(interaction: discord.Interaction, kanal: Place) -> None:
    if not await _admin(interaction):
        return
    assert interaction.guild_id is not None
    blocked.block(interaction.guild_id, kanal.id)
    await interaction.response.send_message(
        f"{_label(kanal)} je zdaj blokiran: ne bom bral sporočil iz njega.",
        ephemeral=True,
    )


@group.command(
    name="odblokiraj",
    description="Odblokiraj kanal ali kategorijo, blokiranega na tem strežniku",
)
@app_commands.describe(kanal="Kanal, kategorija, forum ali nit, ki jo odblokiraš")
async def odblokiraj(interaction: discord.Interaction, kanal: Place) -> None:
    if not await _admin(interaction):
        return
    assert interaction.guild_id is not None
    if kanal.id in blocked.BLOCKED_CHANNELS:
        await interaction.response.send_message(
            f"{_label(kanal)} je blokiran v .env (BLOCKED_CHANNELS). Odstrani ga tam, če ga hočeš odblokirati.",
            ephemeral=True,
        )
        return
    if blocked.unblock(interaction.guild_id, kanal.id):
        reply = f"{_label(kanal)} ni več blokiran."
    else:
        reply = f"{_label(kanal)} ni bil blokiran na tem strežniku."
    await interaction.response.send_message(reply, ephemeral=True)


@group.command(
    name="ucenje", description="Nastavi učenje sloga iz sporočil tega strežnika"
)
@app_commands.describe(
    akcija="Kaj naj naredim",
    kanali="Neobvezno: kanali, iz katerih naj se učim, ločeni z vejico ali presledkom (npr. 'general, memes')",
)
@app_commands.choices(
    akcija=[
        app_commands.Choice(name="vklop", value="vklop"),
        app_commands.Choice(name="izklop", value="izklop"),
        app_commands.Choice(name="znova", value="znova"),
        app_commands.Choice(name="vsi_kanali", value="vsi_kanali"),
    ]
)
async def ucenje(
    interaction: discord.Interaction, akcija: str, kanali: str | None = None
) -> None:
    if not await _admin(interaction):
        return
    scope = _scope(interaction)
    names = [name for name in re.split(r"[,\s]+", kanali or "") if name] or None
    if akcija == "vklop":
        saved = store.configure_style(scope, enabled=True, channels=names)
        reply = f"Učenje sloga je vklopljeno, iz {_where(saved)}."
    elif akcija == "izklop":
        store.configure_style(scope, enabled=False)
        reply = "Učenje sloga je izklopljeno in naučeni vodnik je izbrisan."
    elif akcija == "znova":
        saved = store.configure_style(scope, relearn=True, channels=names)
        reply = f"Učenje sloga se bo znova naučilo iz {_where(saved)}."
    else:  # vsi_kanali
        saved = store.configure_style(scope, enabled=True, all_channels=True)
        reply = f"Učil se bom iz {_where(saved)}."
    await interaction.response.send_message(reply, ephemeral=True)


@group.command(name="spomini", description="Seznam spominov tega strežnika")
async def spomini(interaction: discord.Interaction) -> None:
    if not await _admin(interaction):
        return
    saved = store.memories(_scope(interaction))
    text = (
        "\n".join(f"#{m.id}: {m.text} (od {m.author})" for m in saved)
        if saved
        else "Še ni shranjenih spominov."
    )
    await interaction.response.send_message(_clip(text), ephemeral=True)


@group.command(name="pozabi", description="Izbriši spomin #id tega strežnika")
@app_commands.describe(id="Številka spomina, ki ga izbrišem")
async def pozabi(interaction: discord.Interaction, id: int) -> None:
    if not await _admin(interaction):
        return
    if store.delete_memory(_scope(interaction), id):
        reply = f"Spomin #{id} je izbrisan."
    else:
        reply = f"Spomina #{id} ni."
    await interaction.response.send_message(reply, ephemeral=True)


@group.command(name="naloge", description="Seznam aktivnih nalog tega strežnika")
async def naloge(interaction: discord.Interaction) -> None:
    if not await _admin(interaction):
        return
    tasks = store.tasks(_scope(interaction))
    if not tasks:
        text = "Ni aktivnih nalog."
    else:
        lines = []
        for task in tasks:
            when = task.next_run.astimezone(TIMEZONE)
            schedule = task.cron or "enkrat"
            lines.append(
                f"#{task.id}: {schedule}, naslednjič {when:%Y-%m-%d %H:%M} ({TIMEZONE.key}), "
                f"od {task.user_name}: {task.prompt[:100]}"
            )
        text = "\n".join(lines)
    await interaction.response.send_message(_clip(text), ephemeral=True)


@group.command(name="preklici", description="Prekliči nalogo #id tega strežnika")
@app_commands.describe(id="Številka naloge, ki jo prekličem")
async def preklici(interaction: discord.Interaction, id: int) -> None:
    if not await _admin(interaction):
        return
    if store.cancel_task(_scope(interaction), id):
        reply = f"Naloga #{id} je preklicana."
    else:
        reply = f"Aktivne naloge #{id} ni."
    await interaction.response.send_message(reply, ephemeral=True)


@app_commands.command(
    name="zamujeno",
    description="Povzetek tega, kar si zamudil v kanalu (vidiš ga samo ti)",
)
@app_commands.guild_only()
@app_commands.describe(kanal="Neobvezno: kanal ali nit; privzeto je trenutni kanal")
async def zamujeno(
    interaction: discord.Interaction,
    kanal: discord.TextChannel | discord.Thread | None = None,
) -> None:
    """Privately summarize what the asker missed in a channel, with one fresh agent turn (catch_up)."""
    place = kanal or interaction.channel
    if blocked.channel(place):
        await interaction.response.send_message(
            f"{_label(place)} je blokiran: ne berem sporočil iz njega.", ephemeral=True
        )
        return
    assert interaction.guild_id is not None  # guild_only
    await interaction.response.defer(ephemeral=True, thinking=True)

    scope = _scope(interaction)
    user_name = who(interaction.user)
    permissions = getattr(interaction.user, "guild_permissions", None)
    request = Request(
        scope=scope,
        channel_id=interaction.channel_id,
        user_id=interaction.user.id,
        user_name=user_name,
        message_id=None,  # no asking message: read up to now
        is_admin=bool(permissions and permissions.manage_guild),
    )
    name = getattr(place, "name", None) or str(place)
    content = [
        {
            "type": "text",
            "text": "\n\n".join(
                filter(
                    None,
                    [
                        bot.now_line(),
                        memory_prompt(scope),
                        style_prompt(scope),
                        (
                            f"[/zamujeno] {user_name} asks what they missed in #{name}. "
                            f'Call catch_up with channel "{place.id}" and summarize it for them.'
                        ),
                    ],
                )
            ),
        }
    ]

    client = interaction.client
    session_id: str | None = None
    try:
        session_id, state = await client.run_turn(client.agent, None, request, content)
        text, files = await bot.render(bot.parse(state, session_id))
        text = await untag(interaction.guild, text)
        chunks = bot.split(text) or [""]
        for i, chunk in enumerate(chunks):
            await interaction.followup.send(
                chunk,
                files=files if i == len(chunks) - 1 else [],
                ephemeral=True,
            )
    except Exception:
        log.exception("/zamujeno failed for %s", user_name)
        await interaction.followup.send(ERROR, ephemeral=True)
    finally:
        if session_id is not None:
            # An ephemeral reply can't be replied to, so the session is not kept.
            await client.end_session(session_id)


def setup(tree: app_commands.CommandTree) -> None:
    tree.add_command(zamujeno)
    tree.add_command(group)
