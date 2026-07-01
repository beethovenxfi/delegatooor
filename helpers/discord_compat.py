# helpers/discord_compat.py
import discord


class InteractionCtx:
    """
    Adapter that lets the existing prefix-command logic (which calls
    `ctx.send(...)`) run unchanged under slash commands.

    Slash handlers `await interaction.response.defer()` first, then wrap the
    interaction here. Every `.send(...)` is routed to `interaction.followup.send(...)`,
    which supports content, embeds and file attachments just like `ctx.send`.
    """

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction

    async def send(self, content=None, *, embed=None, file=None, **kwargs):
        # Only forward args that were actually supplied. Passing embed=None / file=None
        # explicitly makes discord.py try to serialize None as an attachment and crash.
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        return await self.interaction.followup.send(**kwargs)
