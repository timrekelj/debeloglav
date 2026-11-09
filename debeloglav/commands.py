"""The /zamujeno slash command: a private catch-up for everyone, answered ephemerally.

Server settings are changed in the admin dashboard, not through slash commands.
"""

import logging

import discord
from discord import app_commands

from . import blocked, bot
from .agent import memory_prompt, style_prompt
from .context import Request
from .mentions import untag, who

log = logging.getLogger("debeloglav")

ERROR = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek."


def _scope(interaction: discord.Interaction) -> str:
    return f"guild:{interaction.guild_id}"


def _label(place: discord.abc.GuildChannel | discord.Thread) -> str:
    return getattr(place, "mention", None) or f"#{place.name}"


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
        private=True,  # the reply is ephemeral, so only the asker sees it
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
