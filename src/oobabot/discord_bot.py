# -*- coding: utf-8 -*-
"""
Main bot class. Contains Discord-specific code that can't
be easily extracted into a cross-platform library.
"""

import asyncio
from collections import deque
import io
import re
import time
import typing
from PIL import Image

import emoji
import discord
import pysbd

from oobabot import bot_commands
from oobabot import decide_to_respond
from oobabot import discord_utils
from oobabot import fancy_logger
from oobabot import image_generator
from oobabot import ooba_client
from oobabot import persona
from oobabot import templates
from oobabot import prompt_generator
from oobabot import repetition_tracker
from oobabot import response_stats
from oobabot import types
from oobabot import vision


class MessageQueue:
    """
    Holds a double-ended message queue for each channel we respond in,
    as well as a dictionary of wait tasks and response tasks per
    channel.

    This is so we can localize our responses, decisions, and actions
    to each specific channel and avoid actions in one channel causing
    strange behavior in other channels.
    """

    def __init__(
        self,
        discord_settings: dict[str, typing.Any]
    ) -> None:
        self.message_accumulation_period: float = round(
            discord_settings["message_accumulation_period"], 1
        )
        self.continue_on_additional_messages: int = discord_settings[
            "continue_on_additional_messages"
        ]
        self.respond_to_latest_only: bool = discord_settings[
            "respond_to_latest_only"
        ]
        self.skip_in_progress_responses: bool = discord_settings[
            "skip_in_progress_responses"
        ]

        self.queues: typing.Dict[int, typing.Deque[discord.Message]] = {}
        self.buffers: typing.Dict[int, typing.Deque[discord.Message]] = {}
        self.response_tasks: typing.Dict[int, asyncio.Task] = {}
        self.wait_tasks: typing.Dict[int, asyncio.Task] = {}


    async def _accumulate_messages(self, channel_id: int) -> None:
        """
        Wait for the configured message accumulation period, then return once it
        has elapsed, or the configured number of additional messages have been
        received.
        """
        if self.continue_on_additional_messages:
            queue_length = self.get_queue_length(channel_id)
            start_time = time.time()
            while (
                self.get_queue_length(channel_id) < (
                    queue_length + self.continue_on_additional_messages + 1
                )
                and time.time() < start_time + self.message_accumulation_period
            ):
                await asyncio.sleep(0.1)
                queue_length = self.get_queue_length(channel_id)
        else:
            await asyncio.sleep(self.message_accumulation_period)


    # Methods to keep track of tasks and check their status easily
    async def buffer(
        self, channel_id: int, message: discord.Message
    ) -> bool:
        """
        Buffer messages for the message accumulation period,
        then flush the buffer to the queue once elapsed.

        The first call to this method will block until the
        message accumulation period has elapsed and then
        return True, while subsequent calls during this period
        will queue messages into the buffer and return False.
        """
        task_created = False
        if channel_id not in self.buffers:
            self.buffers[channel_id] = deque()
        self.buffers[channel_id].appendleft(message)
        if not self.is_buffering(channel_id):
            self.wait_tasks[channel_id] = asyncio.create_task(
                self._accumulate_messages(channel_id)
            )
            task_created = True
            await self.wait_tasks[channel_id]
            self.wait_tasks.pop(channel_id, None)
            if self.respond_to_latest_only:
                self.appendleft(channel_id, self.buffers.pop(channel_id).popleft())
            else:
                self.extendleft(channel_id, self.buffers.pop(channel_id))
        return task_created

    def unbuffer(self, channel_id: int, message: discord.Message) -> None:
        """
        Removes the provided message from the specified
        channel's message buffer, if it exists.
        """
        if (
            channel_id in self.buffers
            and message in self.buffers[channel_id]
        ):
            self.buffers[channel_id].remove(message)

    def is_buffered(self, channel_id: int, message: discord.Message) -> bool:
        """
        Check if the provided message is currently buffered
        for the provided channel.
        """
        if channel_id in self.buffers:
            return message in self.buffers
        return False

    def is_buffering(self, channel_id: int) -> bool:
        """
        Checks if we are currently accumulating messages in the
        specified channel.
        """
        if channel_id in self.wait_tasks:
            return not self.wait_tasks[channel_id].done()
        return False

    def add_response_task(
        self, channel_id: int, response_coro: typing.Coroutine
    ) -> None:
        """
        Schedules the provided coroutune for execution with asyncio and
        stores the resulting task. The task will be removed once it is done.
        """
        self.response_tasks[channel_id] = asyncio.create_task(response_coro)
        self.response_tasks[channel_id].add_done_callback(
            lambda _: self._done_callback(channel_id)
        )

    def remove_response_task(self, channel_id: int) -> None:
        """
        Removes the specified channel's response task, if any.
        """
        self.response_tasks.pop(channel_id, None)

    def get_response_task(
        self, channel_id: int
    ) -> typing.Optional[asyncio.Task]:
        """
        Returns the specified channel's response task, if any.
        """
        return self.response_tasks.get(channel_id, None)

    def cancel_response_task(self, channel_id: int) -> bool:
        """
        Cancels the specified channel's response task, if any.
        """
        if (
            channel_id in self.response_tasks
            and not self.response_tasks[channel_id].done()
        ):
            return self.response_tasks[channel_id].cancel()
        return False

    def is_responding(self, channel_id: int) -> bool:
        """
        Checks if the specified channel has an ongoing response task.
        """
        if channel_id in self.response_tasks:
            return not self.response_tasks[channel_id].done()
        return False

    def get_queue(
        self, channel_id: int
    ) -> typing.Optional[typing.Deque[discord.Message]]:
        """
        Returns the specified channel's message queue, if any.
        """
        return self.queues.get(channel_id, None)

    def remove_queue(self, channel_id: int) -> None:
        """
        Removes the specified channel's message queue, if any.
        """
        self.queues.pop(channel_id, None)

    def contains_message(
        self, channel_id: int, message: discord.Message
    ) -> bool:
        """
        Checks if the specified channel's message queue contains
        the provided message, if the queue exists, otherwise
        return False.
        """
        if channel_id in self.queues:
            return message in self.queues[channel_id]
        return False

    # Standard deque methods but per channel
    def append(self, channel_id: int, message: discord.Message) -> None:
        self._ensure_queue(channel_id).append(message)

    def appendleft(self, channel_id: int, message: discord.Message) -> None:
        self._ensure_queue(channel_id).appendleft(message)

    def pop(self, channel_id: int) -> discord.Message:
        if channel_id in self.queues:
            message = self.queues[channel_id].pop()
            self._remove_empty_queue(channel_id)
            return message
        raise ValueError(f"Channel ID {channel_id} has no queue.")

    def popleft(self, channel_id: int) -> discord.Message:
        if channel_id in self.queues:
            message = self.queues[channel_id].popleft()
            self._remove_empty_queue(channel_id)
            return message
        raise ValueError(f"Channel ID #{channel_id} has no queue.")

    def clear(self, channel_id: int) -> None:
        if channel_id in self.queues:
            self.queues[channel_id].clear()
            return self._remove_empty_queue(channel_id)
        raise ValueError(f"Channel ID #{channel_id} has no queue.")

    def extend(
        self, channel_id: int, messages: typing.Iterable[discord.Message]
    ) -> None:
        self._ensure_queue(channel_id).extend(messages)

    def extendleft(
        self, channel_id: int, messages: typing.Iterable[discord.Message]
    ) -> None:
        self._ensure_queue(channel_id).extendleft(messages)

    def remove(self, channel_id: int, message: discord.Message) -> None:
        if channel_id in self.queues:
            self.queues[channel_id].remove(message)
            return self._remove_empty_queue(channel_id)
        raise ValueError(f"Channel ID #{channel_id} has no queue.")

    def count(self, channel_id: int, message: discord.Message) -> int:
        if channel_id in self.queues:
            return self.queues[channel_id].count(message)
        return 0

    def insert(
        self, channel_id: int, index: int, message: discord.Message
    ) -> None:
        self._ensure_queue(channel_id).insert(index, message)

    def get_queue_length(self, channel_id: int) -> int:
        if channel_id in self.queues:
            return len(self.queues[channel_id])
        return 0

    def __len__(self) -> int:
        # Return the total number of messages across all channels
        return sum(len(queue) for queue in self.queues.values())

    def __getitem__(self, key: typing.Tuple[int, int]) -> discord.Message:
        """
        Takes a tuple of (channel_id, item_index)
        """
        channel_id, index = key
        if channel_id in self.queues:
            return self.queues[channel_id][index]
        raise ValueError(f"Channel ID #{channel_id} has no queue.")

    def __setitem__(
        self, key: typing.Tuple[int, int], value: discord.Message
    ) -> None:
        """
        Takes a tuple of (channel_id, item_index), and the value to set.
        """
        channel_id, index = key
        self._ensure_queue(channel_id)[index] = value

    def __delitem__(self, key: typing.Tuple[int, int]) -> None:
        """
        Takes a tuple of (channel_id, item_index)
        """
        channel_id, index = key
        if channel_id in self.queues:
            del self.queues[channel_id][index]
            self._remove_empty_queue(channel_id)

    def _ensure_queue(self, channel_id: int) -> typing.Deque[discord.Message]:
        """
        Returns the message queue for the specified channel, creating one if
        necessary.
        """
        if channel_id not in self.queues:
            self.queues[channel_id] = deque()
        return self.queues[channel_id]

    def _remove_empty_queue(self, channel_id: int) -> None:
        """
        Check if the queue for a channel is empty and if so, remove the queue.
        """
        if (
            channel_id in self.queues
            and not self.queues[channel_id]
        ):
            self.remove_queue(channel_id)

    def _done_callback(self, channel_id: int) -> None:
        self.remove_response_task(channel_id)
        self._remove_empty_queue(channel_id)

class DiscordBot(discord.Client):
    """
    Main bot class. Connects to Discord, monitors for messages,
    and dispatches responses.
    """

    def __init__(
        self,
        bot_commands: bot_commands.BotCommands,
        decide_to_respond: decide_to_respond.DecideToRespond,
        discord_settings: dict,
        image_generator: typing.Optional[image_generator.ImageGenerator],
        vision_client: typing.Optional[vision.VisionClient],
        ooba_client: ooba_client.OobaClient,
        persona: persona.Persona,
        template_store: templates.TemplateStore,
        prompt_generator: prompt_generator.PromptGenerator,
        repetition_tracker: repetition_tracker.RepetitionTracker,
        response_stats: response_stats.AggregateResponseStats,
    ):
        self.bot_commands = bot_commands
        self.decide_to_respond = decide_to_respond
        self.image_generator = image_generator
        self.vision_client = vision_client
        self.ooba_client = ooba_client
        self.persona = persona
        self.template_store = template_store
        self.prompt_generator = prompt_generator
        self.repetition_tracker = repetition_tracker
        self.response_stats = response_stats

        self.bot_user_id = discord_utils.get_user_id_from_token(discord_settings["discord_token"])
        self.message_character_limit = 2000

        self.dont_split_responses = discord_settings["dont_split_responses"]
        self.ignore_dms = discord_settings["ignore_dms"]
        self.ignore_prefixes = discord_settings["ignore_prefixes"]
        allowed_mentions = [x.lower() for x in discord_settings["allowed_mentions"]]
        for allowed_mention_type in allowed_mentions:
            if allowed_mention_type not in ["everyone", "users", "roles"]:
                raise ValueError(
                    f"Unrecognised allowed mention type '{allowed_mention_type}'. "
                    + "Please fix your configuration."
                )
        # build allowed mentions object from configuration
        self._allowed_mentions = discord.AllowedMentions(
            everyone="everyone" in allowed_mentions,
            users="users" in allowed_mentions,
            roles="roles" in allowed_mentions,
        )
        self.reply_in_thread = discord_settings["reply_in_thread"]
        self.use_immersion_breaking_filter = discord_settings["use_immersion_breaking_filter"]
        self.stop_markers = self.ooba_client.get_stopping_strings()
        self.stop_markers.extend(discord_settings["stop_markers"])
        self.prevent_impersonation = discord_settings["prevent_impersonation"].lower()
        if (
            self.prevent_impersonation
            and self.prevent_impersonation not in ["standard", "aggressive", "comprehensive"]
        ):
            raise ValueError(
                f"Unknown value '{self.prevent_impersonation}' for `prevent_impersonation`. "
                + "Please fix your configuration."
            )
        self.stream_responses = discord_settings["stream_responses"].lower()
        if self.stream_responses and self.stream_responses not in ["token", "sentence"]:
            raise ValueError(
                f"Unknown value '{self.stream_responses}' for `stream_responses`. "
                + "Please fix your configuration."
            )
        self.stream_responses_speed_limit = discord_settings["stream_responses_speed_limit"]

        # Identify our intents with the Gateway
        super().__init__(intents=discord_utils.get_intents())

        # Instantiate the per-channel double-ended message queue
        self.message_queue = MessageQueue(discord_settings)

        # Get a sentence segmenter ready
        self.sentence_splitter = pysbd.Segmenter(language="en", clean=False)
        # and set a regex pattern that we will use to split lines apart. Avoids code
        # duplication in each method where we do this. This must not be a raw string,
        # otherwise the str.strip() method can't use it properly.
        self.line_split_pattern = "\r\n\t\f\v"

        # Compile some regex patterns we will use in the immersion-breaking filter to
        # detect if the AI looks like it is continuing the conversation as someone else,
        # or breaking immersion by giving itself a line prefixed with its name.
        name_identifier = "%%%%%%%%NAME%%%%%%%%"
        user_name_pattern = self.template_store.format(
            templates.Templates.USER_PROMPT_HISTORY_BLOCK,
            {
                templates.TemplateToken.USER_NAME: self.template_store.format(
                    templates.Templates.USER_NAME,
                    {
                        templates.TemplateToken.NAME: name_identifier,
                    },
                ),
                templates.TemplateToken.MESSAGE: "",
            },
        ).strip("\n")
        # Discord usernames are 2-32 characters long, and can only contain special
        # characters '_' and '.' but display names are 1-32 characters long and can
        # contain almost anything, so we try to account for the more permissive option.
        # Hopefully results in fewer false positives than matching anything. Using a
        # prompt history block like "[{USER_NAME}]: {MESSAGE}" will work better.
        user_name_pattern = re.escape(user_name_pattern).replace(
            name_identifier, r"[\S ]{1,32}"
        )
        bot_name_pattern = re.escape(
            self.prompt_generator.bot_prompt_block.strip("\n")
        )
        self.user_message_pattern = re.compile(r"^(" + user_name_pattern + r")(.*)$")
        self.bot_message_pattern = re.compile(r"^(" + bot_name_pattern + r")(.*)$")


    async def on_ready(self) -> None:
        guilds = self.guilds
        num_guilds = len(guilds)
        num_channels = sum(len(guild.channels) for guild in guilds)

        if self.user:
            self.bot_user_id = self.user.id
            user_id_str = self.user.name
        else:
            user_id_str = "<unknown>"

        fancy_logger.get().info(
            "Connected to discord as %s (ID: %d)", user_id_str, self.bot_user_id
        )
        fancy_logger.get().debug(
            "monitoring %d channels across %d server(s)", num_channels, num_guilds
        )
        if self.ignore_dms:
            fancy_logger.get().debug("Ignoring DMs")
        else:
            fancy_logger.get().debug("listening to DMs")

        if self.stream_responses:
            fancy_logger.get().debug(
                "Response Grouping: streamed live into a single message"
            )
        elif self.dont_split_responses:
            fancy_logger.get().debug("Response Grouping: returned as whole messages")
        else:
            fancy_logger.get().debug(
                "Response Grouping: split into individual messages"
            )

        fancy_logger.get().debug("AI name: %s", self.persona.ai_name)
        fancy_logger.get().debug("AI persona: %s", self.persona.persona)

        fancy_logger.get().debug(
            "History: %d lines ", self.prompt_generator.history_lines
        )

        if self.stop_markers:
            fancy_logger.get().debug(
                "Stop markers: %s",
                ", ".join(
                    [f"'{stop_marker}'" for stop_marker in self.stop_markers]
                ).replace("\n", "\\n")
            )

        cap = self.decide_to_respond.get_unsolicited_channel_cap()
        cap = str(cap) if cap > 0 else "<unlimited>"
        fancy_logger.get().debug(
            "Unsolicited channel cap: %s",
            cap,
        )

        if self.persona.wakewords:
            fancy_logger.get().debug("Wakewords: %s", ", ".join(self.persona.wakewords))

        self.ooba_client.on_ready()

        if not self.image_generator:
            fancy_logger.get().debug("Stable Diffusion: disabled")
        else:
            self.image_generator.on_ready()

        # we do this at the very end because when you restart
        # the bot, it can take a while for the commands to
        # register
        try:
            # register the commands
            await self.bot_commands.on_ready(self)
        except discord.DiscordException as err:
            fancy_logger.get().warning(
                "Failed to register commands: %s (continuing without commands)", err
            )

        # show a warning if the bot is connected to zero guilds,
        # with a helpful link on how to fix it
        if num_guilds == 0:
            fancy_logger.get().warning(
                "The bot is not connected to any servers. "
                + "Please add the bot to a server here:",
            )
            fancy_logger.get().warning(
                discord_utils.generate_invite_url(self.bot_user_id)
            )

    async def on_message(self, raw_message: discord.Message) -> None:
        """
        Called when a message is received from Discord.

        This method is called for every message that the bot can see.
        It decides whether to respond to the message, and if so,
        queues the message for processing.

        :param raw_message: The raw message from Discord.
        """

        try:
            # Queue the message and begin processing the queue
            await self.process_messages(raw_message)
        except discord.DiscordException as err:
            fancy_logger.get().error(
                "Error while queueing message for processing: %s: %s",
                type(err).__name__, err, stack_info=True
            )

    async def on_message_delete(self, raw_message: discord.Message) -> None:
        """
        Called when a message in the message cache is deleted from Discord.

        Checks if the deleted message is in our message buffer or queue,
        and removes it if so.
        """
        channel = raw_message.channel
        self.message_queue.unbuffer(channel.id, raw_message)
        if self.message_queue.contains_message(channel.id, raw_message):
            self.message_queue.remove(channel.id, raw_message)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Don't process our own reactions
        if payload.user_id == self.bot_user_id:
            return

        channel = (
            self.get_channel(payload.channel_id)
            or await self.fetch_channel(payload.channel_id)
        )
        if not isinstance(
            channel,
            (
                discord.TextChannel,
                discord.Thread,
                discord.VoiceChannel,
                discord.DMChannel,
                discord.GroupChannel
            )
        ):
            # Don't handle reactions in channel types we don't respond in.
            return
        try:
            raw_message = await channel.fetch_message(payload.message_id)
        # Sometimes the message is already deleted before we can process it, e.g.
        # the PluralKit bot uses ❌ to delete messages too, so we account for that.
        except discord.NotFound:
            return

        no_permission_str = "No MANAGE_MESSAGES permission, cannot remove reaction."
        reactor = (
            payload.member
            or self.get_user(payload.user_id)
            or await self.fetch_user(payload.user_id)
        )

        # hide all chat history at and before this message
        if payload.emoji.name == "⏪":
            fancy_logger.get().debug(
                "Received request from user '%s' to hide chat history in %s.",
                reactor.name,
                discord_utils.get_channel_name(channel)
            )

            # Include the reacted message in the hidden chat history
            self.repetition_tracker.hide_messages_before(
                channel_id=channel.id,
                message_id=payload.message_id
            )
            try:
                # We can't remove reactions on other users' messages in DMs or Group DMs.
                if not isinstance(channel, discord.abc.PrivateChannel):
                    await raw_message.clear_reaction(payload.emoji)
            except discord.NotFound:
                # Also give up if the reaction or message isn't there anymore
                # (i.e. someone removed it before we could), or the original
                # message was deleted.
                return
            except discord.Forbidden:
                fancy_logger.get().warning(no_permission_str)
            return

        # only process the below reactions if it was to one of our messages
        if raw_message.author.id != self.bot_user_id:
            return

        # message deletion
        if payload.emoji.name == "❌":
            fancy_logger.get().debug(
                "Received message deletion request from user '%s' in %s.",
                reactor.name,
                discord_utils.get_channel_name(channel)
            )
            try:
                await raw_message.delete()
            except discord.NotFound:
                # The message was somehow deleted already, ignore and move on.
                pass
            return

        # message regeneration
        if payload.emoji.name == "🔁":
            message = discord_utils.discord_message_to_generic_message(raw_message)
            fancy_logger.get().debug(
                "Received response regeneration request from user '%s' in %s.",
                reactor.name,
                discord_utils.get_channel_name(channel)
            )
            try:
                async with channel.typing():
                    await self._regenerate_response_message(message, raw_message, channel)
                if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
                    return
                try:
                    if not isinstance(channel, discord.abc.PrivateChannel):
                        await raw_message.clear_reaction(payload.emoji)
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    fancy_logger.get().warning(no_permission_str)
            except discord.DiscordException as err:
                fancy_logger.get().error(
                    "Error while regenerating response: %s", err, stack_info=True
                )
                self.response_stats.log_response_failure()

    async def process_messages(
        self,
        raw_message: discord.Message
    ) -> None:
        """
        Queues the provided message, waits for the message accumulation period,
        if configured, and begins processing messages once it has elapsed, or the
        configured number of additional messages have been received while waiting.

        Also filters out any messages that don't match a type we can handle.
        """
        # Allowed message types we can process
        if raw_message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
            discord.MessageType.thread_starter_message
        ):
            return
        # Channel types we can respond in
        channel = raw_message.channel
        if not isinstance(
            channel,
            (
                discord.TextChannel,
                discord.Thread,
                discord.VoiceChannel,
                discord.abc.PrivateChannel
            )
        ):
            return

        # Convert raw message to GenericMessage to perform some operations
        message = discord_utils.discord_message_to_generic_message(raw_message)
        guaranteed = self.decide_to_respond.get_guarantees(channel.id)
        is_guaranteed = guaranteed and raw_message.id in guaranteed
        # Wait if we're accumulating messages. We avoid this in DMs as we assume the 1:1
        # interaction means a response is wanted per-message. Also if the message is a
        # system message.
        if (
            self.message_queue.message_accumulation_period
            and not is_guaranteed
            and not isinstance(channel, discord.DMChannel)
            and raw_message.type in (
                discord.MessageType.default,
                discord.MessageType.reply
            )
            and not self.decide_to_respond.should_ignore_message(
                self.bot_user_id, message
            )
        ):
            # Queue the provided message (unless we should ignore it) and wait
            # if we're beginning accumulation, then proceed with further
            # processing. If we're already accumulating messages, abort and
            # allow the first ongoing task to proceed.
            if not await self.message_queue.buffer(channel.id, raw_message):
                return
        # If we're not accumulating messages, simply queue the message directly.
        else:
            self.message_queue.appendleft(channel.id, raw_message)

        # If there is an ongoing processing task, cancel it, unless we shouldn't
        # respond to the message, otherwise abort, allowing the ongoing task to
        # continue processing the queue.
        if (
            self.message_queue.skip_in_progress_responses
            and raw_message.type in (
                discord.MessageType.default,
                discord.MessageType.reply
            )
            and self.message_queue.is_responding(channel.id)
        ):
            if (
                not is_guaranteed
                and self.decide_to_respond.should_ignore_message(
                    self.bot_user_id, message
                )
            ):
                # We simply abort if this is the case. We don't need to remove the
                # message because the queue processor will ignore it anyway, and
                # we would mutate the queue while it's being iterated, which would
                # result in a RuntimeError.
                return
            # We also give up If the message has been deleted since it was posted
            # (i.e. during the message accumulation period)
            if (
                not self.message_queue.is_buffered(channel.id, raw_message)
                and not self.message_queue.contains_message(channel.id, raw_message)
            ):
                return
            # otherwise, cancel the ongoing task, re-organize the queue and
            # start another processing task.
            if self.message_queue.cancel_response_task(channel.id):
                fancy_logger.get().debug(
                    "Cancelling queued/in-progress responses in %s.",
                    discord_utils.get_channel_name(channel)
                )
                # Wait for the cancelled task to clean up
                cancelled_task = self.message_queue.get_response_task(channel.id)
                if cancelled_task:
                    await cancelled_task
                # Check if the message is in the queue again, after waiting for the
                # cancelled task, which can take a little while.
                if not self.message_queue.contains_message(channel.id, raw_message):
                    return
                # Clear all but the latest message from the queue for this channel
                if (
                    self.message_queue.respond_to_latest_only
                    and self.message_queue.get_queue_length(channel.id) > 1
                ):
                    self.message_queue.clear(channel.id)
                    self.message_queue.appendleft(channel.id, raw_message)
                    # Clean up guaranteed response flags, if any. We must do this here
                    # since it is normally done in the method that just got cancelled
                    # and that may not have happened yet.
                    if guaranteed and len(guaranteed) > 1:
                        guaranteed.clear()
                        if is_guaranteed:
                            guaranteed.add(message.message_id)
                else:
                    self.message_queue.append(channel.id, raw_message)
                    if guaranteed:
                        guaranteed.discard(raw_message.id)

        # Abort if the queue is currently being processed or if there is nothing to process
        if (
            self.message_queue.is_responding(channel.id)
            or not self.message_queue.get_queue_length(channel.id)
        ):
            return
        # otherwise, begin processing message queue
        self.message_queue.add_response_task(
            channel.id, self._process_message_queue(channel.id)
        )

    async def _process_message_queue(self, channel_id: int) -> None:
        """
        Loops through the message queue and responds to each message in received order,
        or if configured, the latest message only.
        """
        # If the queue isn't empty, process the message queue in order of messages received
        message_queue = self.message_queue.get_queue(channel_id)
        while message_queue:
            raw_message = message_queue.pop()

            message = discord_utils.discord_message_to_generic_message(raw_message)
            should_respond, is_summon = self.decide_to_respond.should_respond_to_message(
                self.bot_user_id, message
            )
            if not should_respond:
                continue
            is_summon_in_public_channel = is_summon and isinstance(
                message, types.ChannelMessage
            )

            try:
                await self._handle_response(
                    message,
                    raw_message,
                    is_summon_in_public_channel
                )
            except discord.DiscordException as err:
                fancy_logger.get().error(
                    "Error while processing message: %s: %s",
                    type(err).__name__, err, stack_info=True
                )
        # Purge the response guarantee tracker for this channel once
        # finished processing the queue, just in case a message we
        # didn't process was logged, to prevent memory leaks.
        self.decide_to_respond.purge_guarantees(channel_id)

    async def _handle_response(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        is_summon_in_public_channel: bool,
    ) -> None:
        """
        Called when we've decided to respond to a message.

        It decides if we're sending a text response, an image response,
        or both, and then sends the response(s).
        """
        fancy_logger.get().debug(
            "Message from %s in %s", message.author_name, message.channel_name
        )
        image_prompt = None
        is_image_coming = None

        # Are we creating an image?
        if self.image_generator:
            image_prompt = self.image_generator.maybe_get_image_prompt(message.body_text)
            if image_prompt:
                is_image_coming = await self.image_generator.try_session()

        # Determine if there are images and get descriptions (if Vision is enabled)
        async with raw_message.channel.typing():
            image_descriptions = await self._get_image_descriptions(raw_message)

        # If the message is essentially devoid of content we can handle, abort response.
        if message.is_empty() and not image_descriptions:
            return

        result = await self._send_text_response(
            message=message,
            raw_message=raw_message,
            image_descriptions=image_descriptions,
            image_requested=is_image_coming,
            is_summon_in_public_channel=is_summon_in_public_channel,
        )
        if not result:
            # we failed to create a thread that the user could
            # read our response in, so we're done here. Abort!
            return
        message_task, response_channel = result

        # log the mention, now that we know the channel we
        # want to monitor later to continue to conversation
        if isinstance(response_channel, (discord.Thread, discord.abc.GuildChannel)):
            if is_summon_in_public_channel:
                self.decide_to_respond.log_mention(
                    response_channel.id,
                    message.send_timestamp,
                )

        image_task = None
        if self.image_generator and image_prompt and is_image_coming:
            image_task = self.image_generator.generate_image(
                image_prompt,
                message,
                raw_message,
                response_channel=response_channel,
            )

        response_tasks = [task for task in [message_task, image_task] if task]

        if response_tasks:
            try:
                # We use asyncio.wait instead of asyncio.gather to have more low-level control
                # over task execution and exception handling.
                done, _pending = await asyncio.wait(
                    response_tasks,
                    return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    # Check for exceptions in the tasks that have completed
                    err = task.exception()
                    if err:
                        task_name = task.get_coro().__name__
                        fancy_logger.get().error(
                            "Exception while running %s: %s: %s",
                            task_name, type(err).__name__, err
                        )
            except asyncio.CancelledError:
                if not image_task:
                    for task in response_tasks:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        task_name = task.get_coro().__name__
                        fancy_logger.get().debug("Task '%s' cancelled.", task_name)

    async def _get_image_descriptions(
        self,
        raw_message: discord.Message,
    ) -> typing.List[str]:
        images = []
        image_descriptions = []
        if self.vision_client:
            if self.vision_client.fetch_urls:
                urls = self.vision_client.url_extractor.findall(raw_message.content)
                images += urls
            if raw_message.attachments:
                for attachment in raw_message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        try:
                            # Open our image as a PIL Image object
                            image = Image.open(io.BytesIO(await attachment.read()))
                            # Pre-process the image for the Vision API
                            image = self.vision_client.preprocess_image(image)
                            images.append(image)
                        except Exception as e:
                            fancy_logger.get().error(
                                "Error pre-processing image: %s", e, stack_info=True
                            )
            for image in images:
                try:
                    fancy_logger.get().debug("Getting image description...")
                    description = await self.vision_client.get_image_description(image)
                    if description:
                        image_descriptions.append(description)
                except Exception as e:
                    fancy_logger.get().error("Error processing image: %s", e, stack_info=True)

        return image_descriptions

    async def _generate_text_response(
        self,
        message: types.GenericMessage,
        recent_messages: typing.AsyncIterator,
        image_descriptions: typing.List[str],
        image_requested: typing.Optional[bool],
        response_channel: discord.abc.Messageable,
        as_string: bool = False,
    ) -> typing.Tuple[
        typing.Union[typing.AsyncIterator[str], str], response_stats.ResponseStats
    ]:
        """
        This method is what actually gathers message history, queries the AI for a
        text response, breaks the response into individual messages, and then returns
        a tuple containing either a generator or string, depending on if we're
        spliiting responses or not, and a response stat object.
        """
        fancy_logger.get().debug("Generating prompt...")

        # Convert the recent messages into a list to modify it
        recent_messages_list = [msg async for msg in recent_messages]

        # Attach any image descriptions to the user's message
        if image_descriptions:
            image_received = self.template_store.format(
                templates.Templates.PROMPT_IMAGE_RECEIVED,
                {
                    templates.TemplateToken.AI_NAME: self.persona.ai_name,
                    templates.TemplateToken.USER_NAME: message.author_name,
                },
            )
            description_text = "\n".join(image_received + desc for desc in image_descriptions)
            for msg in recent_messages_list:
                if msg.message_id == message.message_id:
                    # Append the image descriptions to the body text of the user's message
                    msg.body_text += "\n" + description_text
                    break

        # Convert the list back into an async generator
        async def _list_to_async_iter(
            messages: typing.List[types.GenericMessage]
        ) -> typing.AsyncIterator[types.GenericMessage]:
            for message in messages:
                yield message
        recent_messages = _list_to_async_iter(recent_messages_list)

        # Generate the prompt prefix using the modified recent messages
        if isinstance(response_channel, (discord.abc.GuildChannel, discord.Thread)):
            guild_name = response_channel.guild.name
            channel_name = discord_utils.get_channel_name(
                response_channel, with_type=False
            )
        else:
            # DMs are more like channels in a null guild
            guild_name = ""
            channel_name = message.channel_name

        prompt_prefix = await self.prompt_generator.generate(
            bot_user_id=self.bot_user_id,
            message_history=recent_messages,
            guild_name=guild_name,
            channel_name=channel_name,
            image_requested=image_requested
        )
        response_stat = self.response_stats.log_request_arrived(prompt_prefix)

        stopping_strings = []
        if self.prevent_impersonation:
            # Populate a list of stopping strings using the display names of the members
            # who posted most recently, up to the history limit. We do this with a list
            # comprehension which both preserves order, and has linear time complexity
            # vs. quadratic time complexity for loops. We also use a dictionary conversion
            # to de-duplicate instead of checking list membership, as this has constant
            # time complexity vs. linear and also preserves order.
            recent_members = dict.fromkeys([msg.author_name for msg in recent_messages_list])
            # We don't want our own name since our display name isn't used anyway - we always
            # replace it with our configured AI name.
            recent_members.pop(
                self.user.display_name, # type: ignore
                None
            )
            recent_members = recent_members.keys()

            # utility functions to avoid code-duplication and only evaluate when required
            # avoids populating unneeded variables and improves performance very slightly
            def _get_user_prompt_prefix(user_name: str) -> str:
                return self.template_store.format(
                    templates.Templates.USER_PROMPT_HISTORY_BLOCK,
                    {
                        templates.TemplateToken.USER_NAME: user_name,
                        templates.TemplateToken.MESSAGE: "",
                    },
                ).strip()
            def _get_canonical_name(user_name: str) -> str:
                name = emoji.replace_emoji(user_name, "")
                canonical_name = name.split()[0].strip().capitalize()
                return canonical_name if len(canonical_name) >= 3 else name

            for member_name in recent_members:
                user_name = self.template_store.format(
                    templates.Templates.USER_NAME,
                    {
                        templates.TemplateToken.NAME: member_name,
                    },
                )
                if self.prevent_impersonation == "standard":
                    stopping_strings.append(_get_user_prompt_prefix(user_name))
                elif self.prevent_impersonation == "aggressive":
                    stopping_strings.append("\n" + _get_canonical_name(user_name))
                elif self.prevent_impersonation == "comprehensive":
                    stopping_strings.append(_get_user_prompt_prefix(user_name))
                    stopping_strings.append("\n" + _get_canonical_name(user_name))

        fancy_logger.get().debug("Generating text response...")
        try:
            if as_string:
                response = await self.ooba_client.request_as_string(prompt_prefix, stopping_strings)
                return response, response_stat
            if self.stream_responses == "token":
                generator = self.ooba_client.request_as_grouped_tokens(
                    prompt_prefix,
                    stopping_strings,
                    interval=self.stream_responses_speed_limit,
                )
            elif self.stream_responses == "sentence":
                generator = self.ooba_client.request_by_message(
                    prompt_prefix,
                    stopping_strings,
                )
            return generator, response_stat

        except asyncio.CancelledError as err:
            if self.ooba_client.can_abort_generation():
                await self.ooba_client.stop()
            self.response_stats.log_response_failure()
            raise err

    async def _send_text_response(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        image_descriptions: typing.List[str],
        image_requested: typing.Optional[bool],
        is_summon_in_public_channel: bool,
    ) -> typing.Optional[typing.Tuple[asyncio.Task, discord.abc.Messageable]]:
        """
        Send a text response to a message.

        This method determines what channel or thread to post the message
        in, creating a thread if necessary. It then posts the message
        by calling _send_text_response_to_channel().

        Returns a tuple of the task that was created to send the message,
        and the channel that the message was sent to.

        If no message was sent, the task and channel will be None.
        """
        response_channel = raw_message.channel
        if (
            self.reply_in_thread
            and isinstance(response_channel, discord.TextChannel)
            and isinstance(raw_message.author, discord.Member)
        ):
            # we want to create a response thread, if possible
            # but we have to see if the user has permission to do so
            # if the user can't we wont respond at all.
            perms = response_channel.permissions_for(raw_message.author)
            if perms.create_public_threads:
                response_channel = await raw_message.create_thread(
                    name=self.persona.ai_name + " replying to "
                    + message.author_name,
                )
                fancy_logger.get().debug(
                    "Created response thread %s (%d) in %s",
                    response_channel.name,
                    response_channel.id,
                    message.channel_name,
                )
            else:
                # This user can't create threads, so we won't respond.
                # The reason we don't respond in the channel is that
                # it can create confusion later if a second user who
                # DOES have thread-create permission replies to that
                # message. We'd end up creating a thread for that
                # second user's response, and again for a third user,
                # etc.
                fancy_logger.get().debug("User can't create threads, not responding.")
                return None

        response_coro = self._send_text_response_in_channel(
            message=message,
            raw_message=raw_message,
            image_descriptions=image_descriptions,
            image_requested=image_requested,
            is_summon_in_public_channel=is_summon_in_public_channel,
            response_channel=response_channel, # type: ignore
        )
        response_task = asyncio.create_task(response_coro)
        return response_task, response_channel

    async def _send_text_response_in_channel(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        image_descriptions: typing.List[str],
        image_requested: typing.Optional[bool],
        is_summon_in_public_channel: bool,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
    ) -> None:
        """
        Getting closer now! This method requests a text response from the API and then
        sends the message appropriately according to the configured response mode, i.e.
        if we're streaming the response, or sending it all at once.
        """

        repeated_id = self.repetition_tracker.get_throttle_message_id(
            response_channel.id
        )

        # determine if we're responding to a specific message that
        # summoned us. If so, find out what message ID that was, so
        # that we can ignore all messages sent after it (as not to
        # confuse the AI about what to reply to)
        reference = None
        if is_summon_in_public_channel:
            # we can't use the message reference if we're starting a new thread
            if message.channel_id == response_channel.id:
                reference = raw_message.to_reference()
        ignore_all_until_message_id = message.message_id

        recent_messages = await self._recent_messages_following_thread(
            channel=response_channel,
            num_history_lines=self.prompt_generator.history_lines,
            stop_before_message_id=repeated_id,
            ignore_all_until_message_id=ignore_all_until_message_id,
        )

        # will be set to true when we abort the response because:
        # - it was empty
        # - it repeated a previous response and we're throttling it
        aborted_by_us = False
        sent_message_count = 0
        # Show typing indicator in Discord
        async with response_channel.typing():
            # will return a string or generator based on configuration
            response, response_stat = await self._generate_text_response(
                message=message,
                recent_messages=recent_messages,
                image_descriptions=image_descriptions,
                image_requested=image_requested,
                response_channel=response_channel,
                as_string=self.dont_split_responses and not self.stream_responses,
            )

            try:
                if self.stream_responses == "token":
                    sent_message_count, aborted_by_us = await self._render_streaming_response(
                        response, # type: ignore
                        response_stat,
                        response_channel,
                        self._allowed_mentions,
                        reference,
                    )
                elif self.stream_responses == "sentence":
                    sent_message_count, aborted_by_us = await self._render_response_by_sentence(
                        response, # type: ignore
                        response_stat,
                        response_channel,
                        self._allowed_mentions,
                        reference,
                    )
                else:
                    # Post the whole message at once
                    if self.dont_split_responses:
                        sent_message_count, aborted_by_us = await self._send_response_message(
                            response, # type: ignore
                            response_stat,
                            response_channel,
                            self._allowed_mentions,
                            reference,
                        )
                    # or finally, send the response sentence by sentence
                    # in a new message each time, notifying the channel.
                    else:
                        async for sentence in response: # type: ignore
                            sent_message_count, aborted_by_us = await self._send_response_message(
                                sentence,
                                response_stat,
                                response_channel,
                                self._allowed_mentions,
                                reference
                            )
                            if aborted_by_us:
                                break
                            if sent_message_count:
                                # only use the reference for the first
                                # message in a multi-message chain
                                reference = None
                            await asyncio.sleep(self.stream_responses_speed_limit)

            except discord.DiscordException as err:
                if (
                    isinstance(err, discord.HTTPException)
                    and err.status == 400 and err.code == 50035 # pylint: disable=no-member
                ):
                    # Sometimes it's the case where the message we're responding to gets deleted
                    # between when we received it and when we finished generating a response.
                    # If we're trying to send a message with a reference to a deleted message,
                    # this raises a discord.HTTPException with status 400 (bad request) and
                    # code 50035 (invalid form body - unknown reference). We attempt to prevent
                    # responding to deleted messages as much as possible, but it might still
                    # happen due to the time it takes to handle responses.
                    fancy_logger.get().warning(
                        "Original message was deleted before we could reply. "
                        + "Aborting response."
                    )
                else:
                    fancy_logger.get().error(
                        "Error while sending message: %s", err, stack_info=True
                    )
                self.response_stats.log_response_failure()
                return

        if not sent_message_count:
            if aborted_by_us:
                fancy_logger.get().warning(
                    "No response sent. The AI has generated a message that we have "
                    + "chosen not to send, probably because it was empty or repeated."
                )
            else:
                fancy_logger.get().warning(
                    "An empty response was received from Oobabooga. Please check that "
                    + "the AI is running properly on the Oobabooga server at %s.",
                    self.ooba_client.base_url,
                )
            self.response_stats.log_response_failure()
            return

        response_stat.write_to_log(f"Response to {message.author_name} done!  ")
        self.response_stats.log_response_success(response_stat)

    async def _send_response_message(
        self,
        response: str,
        response_stat: response_stats.ResponseStats,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ],
    ) -> typing.Tuple[int, bool]:
        """
        Given a string that represents an individual response message,
        post it in the given channel.

        It also looks to see if a message contains a termination string,
        and if so it will return False to indicate that we should stop
        the response.

        Also does some bookkeeping to make sure we don't repeat ourselves,
        and to track how many messages we've sent.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we need to abort the response entirely
        """
        response, abort_response = self._filter_immersion_breaking_lines(response)
        sent_message_count = 0
        # Reference cannot be None, so we handle it gracefully
        kwargs = {}
        if reference:
            kwargs["reference"] = reference

        # Hopefully we don't get here often but if we do, split the response
        # into sentences, append them to a response buffer until the next
        # sentence would cause the response to exceed the character limit,
        # then post what we have and continue in a new message.
        if len(response) > self.message_character_limit:
            new_response = ""
            # Split lines and preserve our splitting characters using regex split
            # with a capturing group to return the split character(s) in the list
            lines = re.split(r"([" + self.line_split_pattern + r"]+)", response)
            for line in lines:
                # Sometimes the trailing space at the end of a sentence is kept,
                # sometimes not. We avoid ambiguity by explicity stripping
                # additional whitespace and re-adding a trailing space.
                sentences = [
                    x.strip(" ") + " " for x in self.sentence_splitter.segment(line)
                ]
                for sentence in sentences:
                    if len(new_response + sentence) > self.message_character_limit:
                        fancy_logger.get().debug(
                            "Response exceeded %d character limit by %d "
                            + "characters! Posting current message and continuing "
                            + "in a new message.",
                            self.message_character_limit,
                            len(response) - self.message_character_limit
                        )
                        sent_message = await response_channel.send(
                            new_response,
                            allowed_mentions=self._allowed_mentions,
                            suppress_embeds=True,
                            **kwargs
                        )
                        # Reply to our last message in a chain that tracks the whole response
                        kwargs["reference"] = sent_message
                        sent_message_count += 1
                        new_response = ""
                        # Finally, wait for the configured rate-limit timeout
                        await asyncio.sleep(self.stream_responses_speed_limit)
                    new_response += sentence
            response = new_response

        # We can't send an empty message
        if response:
            sent_message = await response_channel.send(
                response,
                allowed_mentions=allowed_mentions,
                suppress_embeds=True,
                **kwargs
            )
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(sent_message),
            )
            response_stat.log_response_part()
            sent_message_count += 1

        return sent_message_count, abort_response

    async def _regenerate_response_message(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
    ) -> None:
        """
        Regenerates a given message by editing it with updated contents using
        the chat history up to the provided message as the prompt.
        """
        repeated_id = self.repetition_tracker.get_throttle_message_id(
            channel.id
        )
        recent_messages = await self._recent_messages_following_thread(
            channel=channel,
            num_history_lines=self.prompt_generator.history_lines,
            stop_before_message_id=repeated_id,
            ignore_all_until_message_id=message.message_id,
            exclude_ignored_message=True,
        )
        image_descriptions = await self._get_image_descriptions(raw_message)
        response, response_stat = await self._generate_text_response(
            message=message,
            recent_messages=recent_messages,
            image_descriptions=image_descriptions,
            image_requested=None,
            response_channel=channel,
            as_string=self.dont_split_responses and not self.stream_responses,
        )

        try:
            if self.stream_responses == "token":
                await self._render_streaming_response(
                    response, # type: ignore
                    response_stat,
                    channel,
                    self._allowed_mentions,
                    existing_message=raw_message,
                )
            elif self.stream_responses == "sentence":
                await self._render_response_by_sentence(
                    response, # type: ignore
                    response_stat,
                    channel,
                    self._allowed_mentions,
                    existing_message=raw_message,
                )
            else:
                response, _ = self._filter_immersion_breaking_lines(response) # type: ignore
                if response:
                    # If it exceeds the character limit, just truncate it for now,
                    # until I figure out how to best handle sending multiple messages
                    # without upsetting the order of messages too much.
                    if len(response) > self.message_character_limit: # type: ignore
                        fancy_logger.get().debug(
                            "Response exceeded %d character limit by %d characters! "
                            + "Truncating excess.",
                            self.message_character_limit,
                            len(response) - self.message_character_limit # type: ignore
                        )
                        response = response[:self.message_character_limit] # type: ignore
                    await raw_message.edit(
                        content=response, # type: ignore
                        suppress=True
                    )
                    response_stat.log_response_part()
                else:
                    fancy_logger.get().warning(
                        "An empty response was received from Oobabooga. Please check that "
                        + "the AI is running properly on the Oobabooga server at %s.",
                        self.ooba_client.base_url,
                    )
                    self.response_stats.log_response_failure()
                    return

        except discord.DiscordException as err:
            fancy_logger.get().error("Error while regenerating message: %s", err, stack_info=True)
            self.response_stats.log_response_failure()
            return

        self.response_stats.log_response_success(response_stat)
        response_stat.write_to_log(f"Regeneration of message #{message.message_id} done!  ")

    async def _render_streaming_response(
        self,
        response_iterator: typing.AsyncIterator[str],
        response_stat: response_stats.ResponseStats,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ] = None,
        existing_message: typing.Optional[discord.Message] = None,
    ) -> typing.Tuple[int, bool]:
        """
        Renders a streaming response into a message by editing it with updated
        contents each time a new group of response tokens is received.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we aborted the response
        """
        buffer = ""
        response = ""
        last_message = existing_message
        sent_message_count = 0

        async for tokens in response_iterator:
            if not tokens:
                continue
            buffer, abort_response = self._filter_immersion_breaking_lines(buffer + tokens)
            # If we would exceed the character limit, post what we have and start a new message
            if len(buffer) > self.message_character_limit:
                fancy_logger.get().debug(
                    "Response exceeded %d character limit! Posting current "
                    + "message and continuing in a new message.",
                    self.message_character_limit
                )
                buffer = ""
                response = ""
                reference = last_message
                last_message = None
            response, abort_response = self._filter_immersion_breaking_lines(response + tokens)

            # don't send an empty message
            if not response:
                continue

            # if we are aborting a response, we want to at least post
            # the valid parts, so don't abort quite yet.
            if not last_message:
                # Reference cannot be None, so we handle it gracefully
                kwargs = {}
                if reference:
                    kwargs["reference"] = reference
                last_message = await response_channel.send(
                    response,
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    **kwargs
                )
                sent_message_count += 1
            else:
                last_message = await last_message.edit(
                    content=response,
                    allowed_mentions=allowed_mentions,
                    suppress=True,
                )

            # we want to abort the response only after we've sent any valid
            # messages, and potentially removed any partial immersion-breaking
            # lines that we posted when they were in the process of being received.
            if abort_response:
                break

            response_stat.log_response_part()

        if last_message and sent_message_count <= 1:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(last_message),
            )

        return sent_message_count, abort_response

    async def _render_response_by_sentence(
        self,
        response_iterator: typing.AsyncIterator[str],
        response_stat: response_stats.ResponseStats,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ] = None,
        existing_message: typing.Optional[discord.Message] = None,
    ) -> typing.Tuple[int, bool]:
        """
        Renders a streaming response, by sentence, into a message by editing it
        with the updated contents every response streaming interval.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we aborted the response
        """
        response = ""
        last_message = existing_message
        sent_message_count = 0

        async for sentence in response_iterator:
            sentence, abort_response = self._filter_immersion_breaking_lines(sentence)
            if not sentence:
                continue
            sentence = sentence.strip(" ") + " "
            # If we would exceed the character limit, start a new message
            if len(response + sentence) > self.message_character_limit:
                fancy_logger.get().debug(
                    "Response exceeded %d character limit! Posting current "
                    + "message and continuing in a new message.",
                    self.message_character_limit
                )
                response = ""
                reference = last_message
                last_message = None
            response += sentence

            if not last_message:
                kwargs = {}
                if reference:
                    kwargs["reference"] = reference
                last_message = await response_channel.send(
                    response,
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    **kwargs
                )
                sent_message_count += 1
            else:
                last_message = await last_message.edit(
                    content=response,
                    allowed_mentions=allowed_mentions,
                    suppress=True,
                )

            if abort_response:
                break

            response_stat.log_response_part()
            # Wait an interval so we don't hit the rate-limit. This is not done
            # in the ooba_client because the method may be used for other things
            # that don't require waiting.
            await asyncio.sleep(self.stream_responses_speed_limit)

        if last_message and sent_message_count <= 1:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(last_message),
            )

        return sent_message_count, abort_response

    def _filter_immersion_breaking_lines(self, text: str) -> typing.Tuple[str, bool]:
        """
        Given a string that represents an individual response message,
        filter any lines that would break immersion.

        These include lines that include a stop marker, lines that attempt
        to carry on the conversation as a different user, and lines that
        include the bot name prompt.

        Returns the subset of the input string that should be sent, and a
        boolean indicating if we should abort the response entirely, ignoring
        any further lines.
        """
        # Do nothing if the filter is disabled
        if not self.use_immersion_breaking_filter:
            return text, False

        # Split by our line split pattern, preserving the split characters in the list.
        # This makes it easy to re-join them later, without having to guess which
        # character we split at.
        lines = re.split(r"([" + self.line_split_pattern + r"]+)", text)
        good_lines = []
        abort_response = False

        for line in lines:
            if not line.strip(self.line_split_pattern):
                # If our line is composed of only split characters, just append it to
                # good_lines to preserve them, and move on.
                good_lines.append(line)
                continue
            # Split the line by our pysbd segmenter to get individual sentences
            sentences = self.sentence_splitter.segment(line)
            good_sentences = []

            for sentence in sentences:
                # pysbd's whitespace preservation is unreliable. We trim any whitespace
                # and add it back at the end. This has the effect of collapsing
                # consecutive whitespace as well.
                sentence = sentence.strip(" ")

                # Check if the bot looks like it's giving itself an extra line
                bot_message_match = self.bot_message_pattern.match(sentence)
                if bot_message_match:
                    # If the display name matches the bot's name, trim the name portion
                    # and keep the remaining text.
                    bot_name_sequence, remaining_text = bot_message_match.groups()
                    bot_prompt_block = self.prompt_generator.bot_prompt_block.strip("\n")
                    if bot_prompt_block in bot_name_sequence:
                        fancy_logger.get().warning(
                            "Caught '%s' in response, trimming '%s' and continuing",
                            sentence, bot_name_sequence
                        )
                        sentence = remaining_text

                # Otherwise, filter out any potential user impersonation
                user_message_match = self.user_message_pattern.match(sentence)
                if user_message_match:
                    # If the display name is not the bot's name and matches the
                    # user name pattern, assume it is a user name and abort the
                    # response for breaking immersion.
                    fancy_logger.get().warning(
                        "Filtered out '%s' from response, aborting", sentence
                    )
                    abort_response = True
                    break # break out of the sentence processing loop

                # Look for stop markers within a sentence
                for marker in self.stop_markers:
                    if marker in sentence:
                        keep_part, removed = sentence.split(marker, 1)
                        fancy_logger.get().warning(
                            "Caught '%s' in response, trimming '%s' and aborting",
                            sentence, removed
                        )
                        if keep_part:
                            good_sentences.append(keep_part)
                        abort_response = True
                        break

                # If we're aborting the response, stop processing additional sentences.
                if abort_response:
                    break

                # filter out sentences that are entirely made of whitespace/newlines
                if sentence.strip():
                    good_sentences.append(sentence)

            # If we're aborting the response, stop processing additional lines
            if abort_response:
                break

            # Re-join sentences with a space between them
            good_line = " ".join(good_sentences)
            if good_line:
                good_lines.append(good_line)

        return "".join(good_lines), abort_response

    async def _filter_history_message(
      self,
      message: discord.Message,
      stop_before_message_id: typing.Optional[int],
   ) -> typing.Tuple[typing.Optional[types.GenericMessage], bool]:
        """
        Filter out any messages that we don't want to include in the
        AI's history.

        These include:
        - messages generated by our image generator
        - messages at or before the stop_before_message_id
        - messages that have been explicitly hidden by the user

        Also, modify the message in the following ways:
        - if the message is from the AI, set the author name to
            the AI's persona name, not its Discord account name
        - remove <@_0000000_> user-id based message mention text,
            replacing them with @username mentions
        """
        # If we've hit the throttle message, stop and don't add any more history
        if stop_before_message_id and message.id == stop_before_message_id:
            generic_message = discord_utils.discord_message_to_generic_message(message)
            return generic_message, False

        generic_message = discord_utils.discord_message_to_generic_message(message)

        if generic_message.author_id == self.bot_user_id:
            # hack: use the suppress_embeds=True flag to indicate that this message
            # is one we generated as part of a text response, as opposed to an
            # image or application message
            if not message.flags.suppress_embeds:
                # this is a message generated by our image generator
                return None, True

            # Make sure the AI always sees its persona name in the transcript, even
            # if the chat program has it under a different account name.
            generic_message.author_name = self.persona.ai_name

        if isinstance(message.channel, discord.DMChannel):
            fn_user_id_to_name = discord_utils.dm_user_id_to_name(
                self.bot_user_id,
                self.persona.ai_name,
                message.author.display_name,
            )
        elif isinstance(message.channel, (discord.abc.GuildChannel, discord.Thread)):
            fn_user_id_to_name = discord_utils.guild_user_id_to_name(
                message.channel.guild,
            )
            await discord_utils.replace_channel_mention_ids_with_names(
                self,
                generic_message,
            )
        elif isinstance(message.channel, discord.GroupChannel):
            fn_user_id_to_name = discord_utils.group_user_id_to_name(
                message.channel,
            )
        else:
            # We shouldn't ever end up here... give it a function anyway.
            fn_user_id_to_name = discord_utils.dm_user_id_to_name(
                self.bot_user_id,
                self.persona.ai_name,
                message.author.display_name,
            )

        discord_utils.replace_user_mention_ids_with_names(
            generic_message,
            fn_user_id_to_name=fn_user_id_to_name,
        )
        discord_utils.replace_emoji_ids_with_names(
            self,
            generic_message
        )
        return generic_message, True

    async def _filtered_history_iterator(
        self,
        async_iter_history: typing.AsyncIterator[discord.Message],
        stop_before_message_id: typing.Optional[int],
        ignore_all_until_message_id: typing.Optional[int],
        limit: int,
        exclude_ignored_message: bool = False,
    ) -> typing.AsyncIterator[types.GenericMessage]:
        """
        When returning the history of a thread, Discord
        does not include the message that kicked off the thread.

        It will show it in the UI as if it were, but it's not
        one of the messages returned by the history iterator.

        This method attempts to return that message as well,
        if we need it.
        """
        items = 0
        last_returned = None
        ignoring_all = ignore_all_until_message_id is not None
        async for item in async_iter_history:
            if items >= limit:
                return

            if ignoring_all:
                if item.id == ignore_all_until_message_id:
                    ignoring_all = False
                    if exclude_ignored_message:
                        # Skip one more message to make sure we hide whatever
                        # message we're referring to as well
                        continue
                else:
                    # This message was sent after the message we're
                    # responding to, so filter out it as to not confuse
                    # the AI into responding to content from that message
                    # instead.
                    continue

            # Don't include thread creation system messages
            if item.type == discord.MessageType.thread_created:
                continue

            # Don't include hidden messages
            if self.decide_to_respond.is_hidden_message(item.content):
                continue

            last_returned = item
            sanitized_message, allow_more = await self._filter_history_message(
                item,
                stop_before_message_id=stop_before_message_id,
            )
            if sanitized_message:
                yield sanitized_message
                items += 1
            if not allow_more:
                # We've hit a message which requires us to stop
                # and look at more history.
                return

        if last_returned and items < limit:
            # We've reached the beginning of the history, but
            # still have space. If this message was a reply
            # to another message, return that message as well.
            if not last_returned.reference:
                return

            reference = last_returned.reference.resolved

            # The resolved message may be None if the message
            # was deleted
            if reference and isinstance(reference, discord.Message):
                sanitized_message, _ = await self._filter_history_message(
                    reference,
                    stop_before_message_id,
                )
                if sanitized_message:
                    yield sanitized_message

    # When looking through the history of a channel, we'll have a goal
    # of retrieving a certain number of lines of history. However,
    # there are some messages in the history that we'll want to filter
    # out. These include messages that were generated by our image
    # generator, as well as certain messages that will be ignored
    # in order to generate a response for a specific user who
    # @-mentions the bot.
    #
    # This is the maximum number of "extra" messages to retrieve
    # from the history, in an attempt to find enough messages
    # that we can filter out the ones we don't want and still
    # have enough left over to satisfy the request.
    #
    # Note that since the history is returned in reverse order,
    # and each is pulled in only as needed, there's not much of a
    # penalty to making this somewhat large. But still, we want
    # to keep it reasonable.
    MESSAGE_HISTORY_LOOKBACK_BONUS = 20

    async def _recent_messages_following_thread(
        self,
        channel: typing.Union[
            discord.TextChannel,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel,
            discord.Thread
        ],
        stop_before_message_id: typing.Optional[int],
        ignore_all_until_message_id: typing.Optional[int],
        num_history_lines: int,
        exclude_ignored_message: bool = False,
    ) -> typing.AsyncIterator[types.GenericMessage]:
        """
        Gets an async iterator of the chat history, between the limits provided.
        """
        max_messages_to_check = num_history_lines + self.MESSAGE_HISTORY_LOOKBACK_BONUS
        history = channel.history(limit=max_messages_to_check)
        result = self._filtered_history_iterator(
            history,
            limit=num_history_lines,
            stop_before_message_id=stop_before_message_id,
            ignore_all_until_message_id=ignore_all_until_message_id,
            exclude_ignored_message=exclude_ignored_message,
        )

        return result
