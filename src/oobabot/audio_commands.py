# -*- coding: utf-8 -*-
"""
Implementation of commands to join and leave voice channels.
"""
import typing

import discord

from oobabot import discord_utils
from oobabot import fancy_logger
from oobabot import ooba_client
from oobabot import persona
from oobabot import prompt_generator
from oobabot import templates
from oobabot import voice_client
from oobabot import decide_to_respond


class AudioCommands:
    """
    Implementation of commands to join and leave voice channels.
    """

    def __init__(
        self,
        persona: persona.Persona,
        discord_settings: typing.Dict,
        ooba_client: ooba_client.OobaClient,
        prompt_generator: prompt_generator.PromptGenerator,
        decide_to_respond: decide_to_respond.DecideToRespond,
        template_store: templates.TemplateStore,
    ):
        voice_client.VoiceClient.wakewords = persona.wakewords

        self.persona = persona
        self.voice_client: typing.Optional[voice_client.VoiceClient] = None

        voice_client.VoiceClient.discord_settings = discord_settings
        voice_client.VoiceClient.decide_to_respond = decide_to_respond
        voice_client.VoiceClient.persona = persona
        voice_client.VoiceClient.ooba_client = ooba_client
        voice_client.VoiceClient.prompt_generator = prompt_generator
        voice_client.VoiceClient.template_store = template_store

    def _discover_voice_channel(
        self, interaction: discord.Interaction
    ) -> typing.Optional[discord.VoiceChannel]:
        if isinstance(interaction.user, discord.Member):
            # if invoked from a guild channel, join the voice channel
            # the invoker is in, within that guild
            if (
                interaction.user.voice
                and interaction.user.voice.channel
                and isinstance(interaction.user.voice.channel, discord.VoiceChannel)
            ):
                return interaction.user.voice.channel

        # if invoked from a private message, look at all guilds
        # which have both the bot and the invoking user as a member,
        # find find the first such guild where the user is in a voice
        # channel.
        for guild in interaction.user.mutual_guilds:
            # get member of guild
            member = guild.get_member(interaction.user.id)
            if (
                member
                and member.voice
                and member.voice.channel
                and isinstance(member.voice.channel, discord.VoiceChannel)
            ):
                return member.voice.channel
        return None

    def add_commands(self, tree):
        @discord.app_commands.command(
            name="join_voice",
            description=f"Have {self.persona.ai_name} join the voice "
            + "channel you are in right now.",
        )
        async def join_voice(interaction: discord.Interaction):
            if not interaction.user:
                await discord_utils.fail_interaction(interaction)

            fancy_logger.get().debug(
                "%s called by user '%s'",
                interaction.command.name, # type: ignore
                interaction.user.name
            )

            voice_channel = self._discover_voice_channel(interaction)
            if not voice_channel:
                await discord_utils.fail_interaction(
                    interaction, "You must be in a voice channel to use this command"
                )
                return

            # are we already connected to a voice channel?  If so, disconnect
            if self.voice_client:
                fancy_logger.get().debug(
                    "disconnecting from voice channel #%s", self.voice_client.channel
                )
                await self.voice_client.disconnect()
                self.voice_client = None

            await interaction.response.defer(
                ephemeral=True,
                thinking=True,
            )

            try:
                self.voice_client = await voice_channel.connect(
                    cls=voice_client.VoiceClient,
                )
                voice_client.VoiceClient.inviter = interaction.user.display_name

                message = f"Joined voice channel #{voice_channel.name}"
            except (discord.DiscordException, voice_client.VoiceClientError) as err:
                fancy_logger.get().error(
                    "Failed to connect to voice channel #%d: %s", voice_channel.id, err
                )
                message = (
                    f"Failed to connect to voice channel #{voice_channel.name}: {err}"
                )

            await interaction.followup.send(message)

        @discord.app_commands.command(
            name="leave_voice",
            description=f"Have {self.persona.ai_name} leave the "
            + "current voice channel.",
        )
        async def leave_voice(interaction: discord.Interaction):
            if not interaction.user:
                await discord_utils.fail_interaction(interaction)

            fancy_logger.get().debug(
                "/leave_voice called by user: '%s'", interaction.user.name
            )

            # are we already connected to a voice channel?  If so, disconnect
            if not self.voice_client:
                await discord_utils.fail_interaction(
                    interaction, "Not connected to a voice channel"
                )
                return

            channel = self.voice_client.channel

            fancy_logger.get().debug("leaving voice channel #%s", channel)

            await self.voice_client.disconnect()
            self.voice_client = None

            await interaction.response.send_message(
                "Left voice channel",
                ephemeral=True,
                silent=True,
            )

        fancy_logger.get().debug("Registering audio commands")
        tree.add_command(join_voice)
        tree.add_command(leave_voice)
