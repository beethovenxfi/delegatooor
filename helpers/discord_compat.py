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
        return await self.interaction.followup.send(
            content=content, embed=embed, file=file, **kwargs
        )
