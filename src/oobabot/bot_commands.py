# -*- coding: utf-8 -*-
"""
Implementation of the bot's slash commands.
"""
import re
import typing

import discord

from oobabot import audio_commands
from oobabot import decide_to_respond
from oobabot import discord_utils
from oobabot import fancy_logger
from oobabot import ooba_client
from oobabot import persona
from oobabot import prompt_generator
from oobabot import repetition_tracker
from oobabot import templates

from oobabot.http_client import OobaHttpClientError

class BotCommands:
    """
    Implementation of the bot's slash commands.
    """

    def __init__(
        self,
        decide_to_respond: decide_to_respond.DecideToRespond,
        repetition_tracker: repetition_tracker.RepetitionTracker,
        persona: persona.Persona,
        discord_settings: dict,
        template_store: templates.TemplateStore,
        ooba_client: ooba_client.OobaClient,
        prompt_generator: prompt_generator.PromptGenerator,
    ):
        self.decide_to_respond = decide_to_respond
        self.repetition_tracker = repetition_tracker
        self.persona = persona
        self.include_lobotomize_response = discord_settings["include_lobotomize_response"]
        self.respond_in_thread = discord_settings["respond_in_thread"]
        self.history_messages = discord_settings["history_messages"]
        self.template_store = template_store
        self.ooba_client = ooba_client

        (
            self.discrivener_location,
            self.discrivener_model_location,
        ) = discord_utils.validate_discrivener_locations(
            discord_settings["discrivener_location"],
            discord_settings["discrivener_model_location"],
        )
        self.speak_voice_responses = discord_settings["speak_voice_responses"]
        self.post_voice_responses = discord_settings["post_voice_responses"]

        if (
            discord_settings["discrivener_location"]
            and not self.discrivener_location
        ):
            fancy_logger.get().warning(
                "Audio disabled because executable at discrivener_location "
                + "could not be found: %s",
                discord_settings["discrivener_location"]
            )

        if (
            discord_settings["discrivener_model_location"]
            and not self.discrivener_model_location
        ):
            fancy_logger.get().warning(
                "Audio disabled because the discrivener_model_location "
                + "could not be found: %s",
                discord_settings["discrivener_model_location"]
            )

        if not self.discrivener_location or not self.discrivener_model_location:
            self.audio_commands = None
        else:
            self.audio_commands = audio_commands.AudioCommands(
                persona,
                discord_settings,
                ooba_client,
                prompt_generator,
                self.decide_to_respond,
                self.template_store,
                self.speak_voice_responses,
                self.post_voice_responses
            )

    async def on_ready(self, client: discord.Client):
        """
        Register commands with Discord.
        """

        async def get_messageable(
            interaction: discord.Interaction,
        ) -> typing.Optional[
            typing.Union[
                discord.TextChannel,
                discord.Thread,
                discord.VoiceChannel,
                discord.DMChannel,
                discord.GroupChannel
            ]
        ]:
            if interaction.channel_id:
                try:
                    channel = await interaction.client.fetch_channel(
                        interaction.channel_id
                    )
                    if channel:
                        if isinstance(
                            channel,
                            (
                                discord.TextChannel,
                                discord.Thread,
                                discord.VoiceChannel,
                                discord.DMChannel,
                                discord.GroupChannel
                            )
                        ):
                            return channel
                except discord.DiscordException as err:
                    fancy_logger.get().error(
                        "Error while fetching channel for command: %s: %s",
                        type(err).__name__, err
                    )

        async def coerce_message(
            interaction: discord.Interaction, message: str
        ) -> typing.Optional[discord.Message]:
            try:
                channel = await get_messageable(interaction)
                if not channel:
                    await discord_utils.fail_interaction(interaction)
                    return
                id_matcher = r"(?:https?://discord(?:app)?\.com/channels/(\d+)/(\d+)/)?(\d+)"
                message_id = re.match(id_matcher, message)
                if message_id:
                    message_id = int(message_id.group(3))
                    raw_message = await channel.fetch_message(message_id)
                else:
                    raise ValueError()
            except ValueError:
                return await discord_utils.fail_interaction(
                    interaction,
                    "Could not fetch provided message."
                )
            return raw_message

        @discord.app_commands.command(
            name="lobotomize",
            description=f"Erase {self.persona.ai_name}'s memory of any message "
            + "before now in this channel."
        )
        async def lobotomize(interaction: discord.Interaction):
            channel = await get_messageable(interaction)
            if not channel:
                await discord_utils.fail_interaction(interaction)
                return

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            response = self.template_store.format(
                template_name=templates.Templates.COMMAND_LOBOTOMIZE_RESPONSE,
                format_args={
                    templates.TemplateToken.AI_NAME: self.persona.ai_name,
                    templates.TemplateToken.NAME: interaction.user.name,
                }
            )
            await interaction.response.send_message(
                response,
                silent=True,
                suppress_embeds=True
            )
            # find the current message in this channel or the
            # message before that if we're including our response.
            # tell the Repetition Tracker to hide messages
            # before this message
            hide_message = await interaction.original_response()
            if not self.include_lobotomize_response:
                fancy_logger.get().debug("Excluding bot response from chat history.")
                async for message in channel.history(
                    limit=self.history_messages, before=hide_message
                ):
                    hide_message = message
                    break

            self.repetition_tracker.hide_messages_before(
                channel_id=channel.id,
                message_id=hide_message.id
            )

        @discord.app_commands.command(
            name="poke",
            description=f"Prompt {self.persona.ai_name} to write a "
            + "response to the last message."
        )
        @discord.app_commands.describe(
            message=f"Message link or ID to prompt {self.persona.ai_name} "
            + "to respond to."
        )
        async def poke(
            interaction: discord.Interaction,
            message: typing.Optional[str]
        ):
            channel = await get_messageable(interaction)
            if not channel:
                await discord_utils.fail_interaction(interaction)
                return

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            await interaction.response.defer(ephemeral=True)
            raw_message = None
            if message:
                raw_message = await coerce_message(interaction, message)

            if not raw_message:
                async for raw_message in channel.history(limit=self.history_messages):
                    if self.decide_to_respond.is_hidden_message(raw_message.content):
                        continue
                    break
            if not raw_message:
                await discord_utils.fail_interaction(
                    interaction,
                    "Can't find a valid message in the last "
                    + f"{self.history_messages} messages."
                )
                return

            await interaction.delete_original_response()
            # Trigger a poke event with the message we fetched
            client.dispatch("poke", raw_message)

        @discord.app_commands.command(
            name="unpoke",
            description=f"Have {self.persona.ai_name} stop responding "
            + "in the current channel until summoned again."
        )
        async def unpoke(interaction: discord.Interaction):
            channel = await get_messageable(interaction)
            if not channel:
                return await discord_utils.fail_interaction(interaction)
            if isinstance(channel, discord.DMChannel):
                return await discord_utils.fail_interaction(
                    interaction,
                    f"Can't use /{interaction.command.name} in a DM." # type: ignore
                )

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            client.dispatch("unpoke", channel)
            response = self.template_store.format(
                templates.Templates.COMMAND_ACKNOWLEDGEMENT,
                {
                    templates.TemplateToken.AI_NAME: self.persona.ai_name,
                    templates.TemplateToken.USER_NAME: interaction.user.display_name
                }
            )
            await interaction.response.send_message(
                response, ephemeral=True, silent=True
            )

        @discord.app_commands.command(
            name="say",
            description=f"Force {self.persona.ai_name} to say the provided message."
        )
        @discord.app_commands.rename(text_to_send="message")
        @discord.app_commands.describe(
            text_to_send=f"Message to force {self.persona.ai_name} to say."
        )
        async def say(interaction: discord.Interaction, text_to_send: str):
            channel = await get_messageable(interaction)
            if not channel:
                await discord_utils.fail_interaction(interaction)
                return

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            # if respond_in_thread is True, we don't want our bot to
            # speak in guild channels, only threads and private messages
            if self.respond_in_thread:
                if not channel or not isinstance(
                    channel, (discord.abc.PrivateChannel, discord.Thread)
                ):
                    await discord_utils.fail_interaction(
                        interaction,
                        "I may only speak in threads."
                    )
                    return
            self.decide_to_respond.log_mention(
                guild_id=channel.guild.id if channel.guild else channel.id,
                channel_id=interaction.channel_id, # type: ignore
                send_timestamp=interaction.created_at.timestamp()
            )
            await interaction.response.send_message(
                text_to_send,
                suppress_embeds=True
            )

        @discord.app_commands.command(
            name="edit",
            description=f"Replace {self.persona.ai_name}'s most recent "
            + "message in the channel with the provided message."
        )
        @discord.app_commands.describe(
            text=f"Text to replace {self.persona.ai_name}'s last "
            + "message with."
        )
        @discord.app_commands.describe(
            message=f"Message link or ID from {self.persona.ai_name} to edit."
        )
        async def edit(
            interaction: discord.Interaction,
            text: str,
            message: typing.Optional[str]
        ):
            channel = await get_messageable(interaction)
            if not channel:
                await discord_utils.fail_interaction(interaction)
                return

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            await interaction.response.defer(ephemeral=True)
            raw_message = None
            if message:
                raw_message = await coerce_message(interaction, message)

            if not raw_message:
                async for raw_message in channel.history(limit=self.history_messages):
                    if self.decide_to_respond.is_hidden_message(raw_message.content):
                        continue

                    if raw_message.author.id == client.user.id: # type: ignore
                        break
            if not raw_message:
                await discord_utils.fail_interaction(
                    interaction,
                    "Can't find my last message in the last "
                    + f"{self.history_messages} messages."
                )
                return

            self.decide_to_respond.log_mention(
                guild_id=channel.guild.id if channel.guild else channel.id,
                channel_id=interaction.channel_id, # type: ignore
                send_timestamp=interaction.created_at.timestamp()
            )
            await raw_message.edit(content=text, suppress=True)
            await interaction.delete_original_response()

        @discord.app_commands.command(
            name="stop",
            description=f"Force {self.persona.ai_name} to stop typing "
            + "the current message."
        )
        async def stop(interaction: discord.Interaction):
            channel = await get_messageable(interaction)
            if not channel:
                await discord_utils.fail_interaction(interaction)
                return

            channel_name = discord_utils.get_channel_name(channel)
            fancy_logger.get().debug(
                "/%s called by user '%s' in %s",
                interaction.command.name, # type: ignore
                interaction.user.name,
                channel_name
            )

            if not self.ooba_client.can_abort_generation:
                await discord_utils.fail_interaction(
                    interaction,
                    "Current API does not support stopping generation."
                )
                return
            try:
                await self.ooba_client.stop()
            except OobaHttpClientError as err:
                await discord_utils.fail_interaction(
                    interaction,
                    f"Something went wrong! {err}"
                )
                return

            response = self.template_store.format(
                templates.Templates.COMMAND_ACKNOWLEDGEMENT,
                {
                    templates.TemplateToken.AI_NAME: self.persona.ai_name,
                    templates.TemplateToken.USER_NAME: interaction.user.display_name
                }
            )
            await interaction.response.send_message(
                response, ephemeral=True, silent=True
            )


        fancy_logger.get().debug(
            "Registering commands, sometimes this takes a while..."
        )

        tree = discord.app_commands.CommandTree(client)
        tree.add_command(lobotomize)
        tree.add_command(poke)
        tree.add_command(unpoke)
        tree.add_command(say)
        tree.add_command(edit)
        tree.add_command(stop)

        if self.audio_commands:
            self.audio_commands.add_commands(tree)

        commands = await tree.sync()
        for command in commands:
            fancy_logger.get().info(
                "Registered command: %s: %s",
                command.name, command.description
            )
