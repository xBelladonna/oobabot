# -*- coding: utf-8 -*-
"""
Main bot class. Contains Discord-specific code that can't
be easily extracted into a cross-platform library.
"""

import asyncio
from collections import deque
from hashlib import sha256
import io
import re
import time
import typing

import emoji
import discord

from oobabot import bot_commands
from oobabot import decide_to_respond
from oobabot import discord_utils
from oobabot import fancy_logger
from oobabot import image_generator
from oobabot import immersion_breaking_filter
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
        self.panic_duration: float = discord_settings["panic_duration"]

        self.queues: typing.Dict[int, typing.Deque[discord.Message]] = {}
        self.buffers: typing.Dict[int, typing.Deque[discord.Message]] = {}
        self.response_tasks: typing.Dict[int, asyncio.Task] = {}
        self.wait_tasks: typing.Dict[int, asyncio.Task] = {}
        self.panic_tasks: typing.Dict[int, asyncio.Task] = {}


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
            if self.buffers.get(channel_id, None):
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

    async def _panic(self, channel_id: int) -> None:
        """
        Creates a panic task for the specified channel,
        for the configured duration.
        """
        fancy_logger.get().info(
            "Panicking for %.1f seconds...",
            self.panic_duration
        )
        try:
            if self.is_buffering(channel_id):
                self.buffers.pop(channel_id).clear()
            if self.get_queue_length(channel_id):
                self.clear(channel_id)
            if self.is_responding(channel_id):
                self.cancel_response_task(channel_id)
            await asyncio.sleep(self.panic_duration)
            fancy_logger.get().info("Calming down again.")
        except asyncio.CancelledError:
            fancy_logger.get().info("Cancelling panic.")

    def panic(self, channel_id: int) -> None:
        """
        Creates a panic task for the specified channel,
        for the configured duration.
        """
        if self.is_panicking(channel_id):
            return
        self.panic_tasks[channel_id] = asyncio.create_task(
            self._panic(channel_id)
        )
        self.panic_tasks[channel_id].add_done_callback(
            lambda _: self.panic_tasks.pop(channel_id, None)
        )

    async def calm_down(self, channel_id: int) -> None:
        """
        Cancel any ongoing panic in the specified channel.
        """
        if self.is_panicking(channel_id):
            self.panic_tasks[channel_id].cancel()
            await self.panic_tasks[channel_id]

    def is_panicking(self, channel_id: int) -> bool:
        """
        Checks if we're panicking in the specified channel.
        """
        if channel_id in self.panic_tasks:
            return not self.panic_tasks[channel_id].done()
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

        self.split_responses = discord_settings["split_responses"]
        self.ignore_dms = discord_settings["ignore_dms"]
        self.ignore_prefixes = discord_settings["ignore_prefixes"]
        self.ignore_reactions = discord_settings["ignore_reactions"]
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
        self.respond_in_thread = discord_settings["respond_in_thread"]
        self.use_immersion_breaking_filter = discord_settings["use_immersion_breaking_filter"]
        self.retries = discord_settings["retries"]
        if self.retries < 0:
            raise ValueError("Number of retries can't be negative. Please fix your configuration.")
        self.stop_markers = self.ooba_client.get_stop_sequences()
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

        # Log in and identify our intents with the Gateway
        super().__init__(intents=discord_utils.get_intents())

        # Instantiate the per-channel double-ended message queue
        self.message_queue = MessageQueue(discord_settings)

        # Get our immersion-breaking filter ready
        self.immersion_breaking_filter = immersion_breaking_filter.ImmersionBreakingFilter(
            discord_settings, self.prompt_generator, self.template_store
        )

        # Register any custom events
        self.event(self.on_poke)
        self.event(self.on_unpoke)
        self.event(self.on_rewrite_request)


    async def on_ready(self) -> None:
        """
        Called by our runtime once the bot is set up and has successfully
        connected to Discord.
        """
        guilds = self.guilds
        num_guilds = len(self.guilds)
        num_private_channels = len(self.private_channels)
        num_channels = sum(len(guild.channels) for guild in guilds)

        if self.user:
            self.bot_user_id = self.user.id
            user_id_str = self.user.name
            # Discriminator is legacy and is 0 if not present.
            if int(self.user.discriminator):
                user_id_str += "#" + self.user.discriminator
        else:
            user_id_str = "<unknown>"

        fancy_logger.get().info(
            "Connected to Discord as %s (ID: %d)", user_id_str, self.bot_user_id
        )
        fancy_logger.get().debug(
            "Monitoring %d channels across %d server(s)", num_channels, num_guilds
        )
        if self.ignore_dms:
            fancy_logger.get().debug("Ignoring DMs")
        elif num_private_channels:
            fancy_logger.get().debug(
                "Monitoring %d%s DMs",
                num_private_channels,
                # Discord only returns the most recent 128 channels
                "+" if num_private_channels >= 128 else ""
            )
        else:
            fancy_logger.get().debug("Monitoring DMs")

        cap = self.decide_to_respond.unsolicited_channel_cap
        cap = str(cap) if cap > 0 else "<unlimited>"
        fancy_logger.get().debug(
            "Unsolicited channel cap: %s", cap
        )

        fancy_logger.get().debug("AI name: %s", self.persona.ai_name)
        if self.persona.description:
            fancy_logger.get().debug("AI description: %s", self.persona.description)
        if self.persona.personality:
            fancy_logger.get().debug("AI personality: %s", self.persona.personality)
        if self.persona.scenario:
            fancy_logger.get().debug("AI scenario: %s", self.persona.scenario)

        fancy_logger.get().debug(
            "History: %d messages ", self.prompt_generator.history_messages
        )
        if self.stream_responses:
            response_grouping = "streamed live into a single message"
        elif self.split_responses:
            response_grouping = "split into individual messages"
        else:
            response_grouping = "returned as whole messages"
        fancy_logger.get().debug("Response grouping: %s", response_grouping)

        if self.stop_markers:
            fancy_logger.get().debug(
                "Stop markers: %s",
                ", ".join(
                    [f"'{stop_marker}'" for stop_marker in self.stop_markers]
                ).replace("\n", "\\n")
            )

        if self.persona.wakewords:
            fancy_logger.get().debug(
                "Wakewords: %s", ", ".join(
                    [f"'{wakeword}'" for wakeword in self.persona.wakewords]
                )
            )

        self.ooba_client.on_ready()

        if not self.image_generator:
            fancy_logger.get().debug("Stable Diffusion: disabled")
        else:
            self.image_generator.on_ready()
        if not self.vision_client:
            fancy_logger.get().debug("Vision: disabled")

        # show a warning if the bot is not in any channels or DMs,
        # with a helpful link on how to fix it
        if not num_guilds and not num_private_channels:
            fancy_logger.get().warning(
                "The bot is not connected to any servers or DMs. "
                + "Please add the bot to a server here:",
            )
            fancy_logger.get().warning(
                discord_utils.generate_invite_url(self.bot_user_id)
            )

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

    async def on_message(self, raw_message: discord.Message) -> None:
        """
        Called when a message is received from Discord.

        This method is called for every message that the bot can see.
        It queues the incoming messages and starts a processing task.
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
        """
        Called when any message receives a reaction, regardless of whether
        it is in the message cache or not.

        Checks if the reaction is a command, and processes it if so.
        """

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

        # Hide all chat history at and before this message
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

        # Poke by reaction (:point_up_2:)
        if payload.emoji.name == "👆":
            fancy_logger.get().debug(
                "Received poke from user '%s' in %s.",
                reactor.name,
                discord_utils.get_channel_name(channel)
            )
            try:
                if not isinstance(channel, discord.abc.PrivateChannel):
                    await raw_message.clear_reaction(payload.emoji)
            except discord.NotFound:
                pass
            except discord.Forbidden:
                fancy_logger.get().warning(no_permission_str)
            # Abort if the message is hidden
            if self.decide_to_respond.is_hidden_message(raw_message.content):
                return
            await self.on_poke(raw_message)
            return

        # only process the below reactions if it was to one of our messages
        if raw_message.author.id != self.bot_user_id:
            return

        # Message deletion
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

        # Response message regeneration
        if payload.emoji.name == "🔁":
            fancy_logger.get().debug(
                "Received response regeneration request from user '%s' in %s.",
                reactor.name,
                discord_utils.get_channel_name(channel)
            )
            try:
                await self._regenerate_response_message(raw_message, channel)
                if not isinstance(channel, discord.abc.PrivateChannel):
                    try:
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

    async def on_poke(self, raw_message: discord.Message) -> None:
        """
        Cancel any ongoing channel panic and respond to the latest message.
        """
        channel = raw_message.channel
        # Calm down if we're currently panicking
        await self.message_queue.calm_down(channel.id)
        # Ensure we respond to the message and pay attention to the channel
        self.decide_to_respond.guarantee_response(channel.id, raw_message.id)
        self.decide_to_respond.log_mention(
            channel.guild.id if channel.guild else channel.id,
            channel.id,
            raw_message.created_at.timestamp()
        )
        # Trigger an incoming message request
        await self.process_messages(raw_message)

    async def on_unpoke(
        self,
        channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ]
    ) -> None:
        """
        Panic and stop paying attention in the specified channel.
        """
        self.message_queue.panic(channel.id)
        self.decide_to_respond.log_cooldown(
            channel.guild.id
            if channel.guild else channel.id,
            channel.id
        )

    async def on_rewrite_request(
        self,
        raw_message: discord.Message,
        instruction: str
    ) -> None:
        """
        Listens for custom `rewrite_request` events and passes the request through to
        _regenerate_response_message() with the instruction. The AI will be prompted
        with chat history up to the provided message and asked to rewrite its last
        response according to the provided instruction. The contents of the message
        will be replaced with its response.
        """
        fancy_logger.get().debug(
            "Received message rewrite request from user '%s' in %s. "
            + "Rewriting last response...",
            raw_message.author.name,
            discord_utils.get_channel_name(raw_message.channel)
        )
        try:
            await self._regenerate_response_message(
                raw_message,
                raw_message.channel, # type: ignore
                instruction
            )
        except discord.DiscordException as err:
            fancy_logger.get().error(
                "Error while rewriting response: %s", err
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
        # Abort if we're panicking in this channel
        if not is_guaranteed and self.message_queue.is_panicking(channel.id):
            return
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
        Loops through the message queue, decides whether to respond to a message,
        then calls _handle_response() if we're responding. Makes a decision for
        each queued message, or if configured, the latest message only.
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
                message, (types.ChannelMessage, types.GroupMessage)
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

        It decides if we're sending a text response, an image response, or both,
        and then sends the response(s).
        """
        fancy_logger.get().debug(
            "Responding to message from %s in %s",
            message.author_name, message.channel_name
        )
        image_prompt = None
        is_image_coming = None

        # Are we creating an image?
        if self.image_generator:
            image_prompt = self.image_generator.maybe_get_image_prompt(message.body_text)
            if image_prompt:
                is_image_coming = await self.image_generator.try_session()

        result = await self._send_text_response(
            message=message,
            raw_message=raw_message,
            is_summon_in_public_channel=is_summon_in_public_channel,
            image_requested=is_image_coming
        )
        if not result:
            # we failed to create a thread that the user could
            # read our response in, so we're done here. Abort!
            return
        message_task, response_channel = result

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
    ) -> typing.List[types.GenericAttachment]:
        """
        Fetches any message attachments and valid image URLs and gets text descriptions
        for them, if Vision is enabled. If Vision is not enabled or no descriptions were
        generated, return an empty list.
        """
        attachments: typing.List[types.GenericAttachment] = []

        if self.vision_client:
            # Process URLs if we are configured to fetch them
            if self.vision_client.fetch_urls:
                # Get an iterator of URL matches
                urls = self.vision_client.URL_EXTRACTOR.finditer(raw_message.content)
                for url in urls:
                    # Get the whole match as a string
                    url = url.group()
                    if await self.vision_client.is_image_url(url):
                        # If the URL is valid and points to an image, get the image description
                        fancy_logger.get().debug("Getting image description...")
                        try:
                            async with raw_message.channel.typing():
                                description = await self.vision_client.get_image_description(url)
                        except Exception as err:
                            fancy_logger.get().error(
                                "Error getting image description: %s: %s",
                                type(err).__name__, err, stack_info=True
                            )
                            continue
                        # Create an Attachment with the raw URL as the content hash
                        attachments.append(
                            types.GenericAttachment(
                                content_type="image_url",
                                description_text=description,
                                content_hash=url
                            )
                        )
            # then process any message attachments
            for attachment in raw_message.attachments:
                if (
                    attachment.content_type
                    and attachment.content_type.startswith("image/")
                ):
                    try:
                        # Read our image into a BytesIO buffer
                        image_buffer = io.BytesIO(await attachment.read())
                        # Pre-process the image for the Vision API
                        image_base64 = self.vision_client.preprocess_image(image_buffer)
                        # If we got a valid base64 image, get the image description
                        fancy_logger.get().debug("Getting image description...")
                        async with raw_message.channel.typing():
                            description = \
                                await self.vision_client.get_image_description(image_base64)
                            # Rewind the buffer
                            image_buffer.seek(0)
                            # Create an Attachment with the sha256 hash of the raw image as
                            # the content hash
                            attachments.append(
                                types.GenericAttachment(
                                    content_type="image",
                                    description_text=description,
                                    content_hash=sha256(image_buffer.read()).hexdigest()
                                )
                            )
                    except Exception as err:
                        fancy_logger.get().error(
                            "Error getting image description: %s: %s",
                            type(err).__name__, err, stack_info=True
                        )

        return attachments

    async def _generate_text_response(
        self,
        message: types.GenericMessage,
        recent_messages: typing.AsyncIterator[types.GenericMessage],
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        image_requested: typing.Optional[bool] = None,
        rewrite_request: typing.Optional[str] = None
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

        prompt, author_names = await self.prompt_generator.generate(
            message_history=recent_messages,
            bot_user_id=self.bot_user_id,
            user_name=message.author_name,
            guild_name=guild_name,
            channel_name=channel_name,
            image_requested=image_requested,
            rewrite_request=rewrite_request
        )

        stop_sequences: typing.List[str] = []
        if self.prevent_impersonation:
            # Utility functions to avoid code-duplication
            def _get_user_prompt_prefix(user_name: str) -> str:
                return self.template_store.format(
                    templates.Templates.USER_PROMPT_HISTORY_BLOCK,
                    {
                        templates.TemplateToken.NAME: user_name,
                        templates.TemplateToken.MESSAGE: ""
                    }
                ).strip()
            def _get_canonical_name(user_name: str) -> str:
                name = emoji.replace_emoji(user_name, "")
                canonical_name = name.split()[0].strip().capitalize()
                return canonical_name if len(canonical_name) >= 3 else name

            for author_name in author_names:
                if self.prevent_impersonation == "standard":
                    stop_sequences.append(_get_user_prompt_prefix(author_name))
                elif self.prevent_impersonation == "aggressive":
                    stop_sequences.append("\n" + _get_canonical_name(author_name))
                elif self.prevent_impersonation == "comprehensive":
                    stop_sequences.append(_get_user_prompt_prefix(author_name))
                    stop_sequences.append("\n" + _get_canonical_name(author_name))

        fancy_logger.get().debug("Generating text response...")
        response_stat = self.response_stats.log_request_arrived(prompt)
        try:
            # If we're streaming our response as groups of tokens
            if self.stream_responses == "token":
                generator = self.ooba_client.request_as_grouped_tokens(
                    prompt,
                    stop_sequences,
                    interval=self.stream_responses_speed_limit
                )
                return generator, response_stat
            # If we're splitting or streaming our response by sentence
            if self.stream_responses == "sentence" or self.split_responses:
                generator = self.ooba_client.request_by_message(
                    prompt,
                    stop_sequences
                )
                return generator, response_stat
            # or finally, if we're not splitting our response
            response = await self.ooba_client.request_as_string(prompt, stop_sequences)
            return response, response_stat

        except asyncio.CancelledError as err:
            if self.ooba_client.can_abort_generation():
                await self.ooba_client.stop()
            self.response_stats.log_response_failure()
            raise err

    async def _send_text_response(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        is_summon_in_public_channel: bool,
        image_requested: typing.Optional[bool] = None
    ) -> typing.Optional[typing.Tuple[asyncio.Task, discord.abc.Messageable]]:
        """
        Send a text response to a message.

        This method fetches descriptions for any image attachments or valid image URLs,
        and then determines if we can send a response based on if there is any content.
        If the message was sent with no text content and there are no images, we give up.

        If we're able to respond, we determine what channel or thread to post the message
        in, creating a thread if necessary. We then post the message by calling
        _send_text_response_in_channel().

        Returns a tuple with:
        - the task that was created to send the message
        - the channel that the message was sent to, or None if no message was sent
        """
        # Determine if there are images and get descriptions (if Vision is enabled)
        # We do this here instead of in _send_text_response_in_channel to avoid
        # creating a thread if we end up with no content we can respond to.
        message.attachments += await self._get_image_descriptions(raw_message)
        if message.is_empty():
            return

        # If we were mentioned, log the mention in the original channel
        # to monitor for and respond to further conversation.
        if isinstance(message, (types.ChannelMessage, types.GroupMessage)):
            if is_summon_in_public_channel:
                self.decide_to_respond.log_mention(
                    message.guild_id
                    if isinstance(message, types.ChannelMessage)
                    else message.channel_id,
                    message.channel_id,
                    message.send_timestamp
                )

        response_channel = raw_message.channel
        if (
            self.respond_in_thread
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
                # If we created a new thread, log the mention there too so we
                # can continue the conversation.
                if is_summon_in_public_channel:
                    self.decide_to_respond.log_mention(
                        response_channel.guild.id,
                        response_channel.id,
                        message.send_timestamp,
                    )
            else:
                # This user can't create threads, so we won't respond. The reason we don't
                # respond in the channel is firstly that we aren't configured to, and
                # secondly that it can create confusion later if a second user who DOES
                # have thread-create permission replies to that message. We'd end up
                # creating a thread for that second user's response, and again for a
                # third user, etc.
                fancy_logger.get().warning(
                    "%s can't create threads in %s, not responding.",
                    message.author_name,
                    message.channel_name
                )
                return None

        response_coro = self._send_text_response_in_channel(
            message=message,
            raw_message=raw_message,
            is_summon_in_public_channel=is_summon_in_public_channel,
            response_channel=response_channel, # type: ignore
            image_requested=image_requested
        )
        response_task = asyncio.create_task(response_coro)
        return response_task, response_channel

    async def _send_text_response_in_channel(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        is_summon_in_public_channel: bool,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        image_requested: typing.Optional[bool] = None,
        existing_message: typing.Optional[discord.Message] = None,
        rewrite_request: typing.Optional[str] = None
    ) -> None:
        """
        Getting closer now! This method requests a text response from the API and then
        sends the message appropriately according to the configured response mode, i.e.
        if we're streaming the response, or sending it all at once. If an existing
        message (and optionally rewrite request) is passed, that message will be edited
        first instead of sending a new message immediately.
        """

        tries = range(self.retries + 1) # add offset of 1 as range() is zero-indexed
        retry_throttle_id = 0
        for _retry in tries:
            repeated_id = (
                retry_throttle_id
                or self.repetition_tracker.get_throttle_message_id(
                    response_channel.id
                )
            )
            history_marker_id = self.repetition_tracker.get_history_marker_id(
                response_channel.id
            )

            # If this message is one that summoned us, get a reference to it so we
            # can reply to it.
            reference = None
            if is_summon_in_public_channel:
                # We can't use the message reference if we're starting a new thread
                if message.channel_id == response_channel.id:
                    reference = raw_message.to_reference()
            ignore_all_until_message_id = message.message_id

            recent_messages = self._filtered_history_iterator(
                message=message,
                channel=response_channel,
                stop_before_message_id=repeated_id or history_marker_id,
                ignore_all_until_message_id=ignore_all_until_message_id,
                limit=self.prompt_generator.history_messages
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
                    response_channel=response_channel,
                    image_requested=image_requested,
                    rewrite_request=rewrite_request
                )
                # If we have the whole response at once, we can check for
                # similarity to the last response and retry if too similar.
                if isinstance(response, str) and self.repetition_tracker.repetition_threshold:
                    response_text, aborted_by_us = self.immersion_breaking_filter.filter(
                        response, suppress_logging=True
                    )
                    if response_text.strip():
                        # Check to see if this response is a repetition of the last
                        # message logged with the repetition tracker, without logging
                        # the message itself, since we haven't sent it yet.
                        repeated_message = self.repetition_tracker.is_repetition(
                            response_channel.id, response_text
                        )
                        if repeated_message:
                            # Log a warning and skip this iteration to retry generation
                            # with history throttled behind the last user message.
                            if repeated_message is True:
                                detail_text = "exact match"
                            else:
                                detail_text = f"similarity score: {repeated_message:.2f}"
                            warn_text = (
                                "Response was too similar to the previous response "
                                + f"({detail_text})."
                            )
                            if _retry < self.retries:
                                warn_text += " Regenerating response..."
                            fancy_logger.get().warning(warn_text)
                            retry_throttle_id = message.message_id
                            continue

                # Now send the response after all retries have completed
                try:
                    (
                        sent_message_count, aborted_by_us
                    ) = await self._render_response(
                        response,
                        response_stat,
                        response_channel,
                        reference,
                        existing_message
                    )
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
                            "Error while sending response: %s: %s",
                            type(err).__name__, err, stack_info=True
                        )
                    self.response_stats.log_response_failure()
                    return

            # If we sent any messages, break out of the retry loop
            if sent_message_count:
                break
            # otherwise, log a warning and retry
            if aborted_by_us:
                warn_text = "Response was empty after filtering immersion-breaking lines."
            else:
                warn_text = "An empty text response was received from the API."
            if _retry < self.retries:
                warn_text += " Regenerating response..."
            fancy_logger.get().warning(warn_text)

        # If we reached the maximum number of tries and still didn't send any messages
        if not sent_message_count:
            if aborted_by_us:
                fancy_logger.get().warning(
                    "No response sent after %d tries. The AI has generated a response "
                    + "that we have chosen not to send, probably because it was repeated "
                    + "or broke immersion.",
                    len(tries)
                )
            else:
                fancy_logger.get().warning(
                    "No response sent after %d tries. Giving up.",
                    len(tries)
                )
            self.response_stats.log_response_failure()
            return

        if existing_message:
            response_stat.write_to_log(f"Regeneration of message #{existing_message.id} done!  ")
        else:
            response_stat.write_to_log(f"Response to {message.author_name} done!  ")
        self.response_stats.log_response_success(response_stat)

    async def _regenerate_response_message(
        self,
        raw_message: discord.Message,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        rewrite_request: typing.Optional[str] = None
    ) -> None:
        """
        Regenerates a given response message by editing it with updated
        contents using the chat history up to the provided message as the
        prompt. If `rewrite_request` is provided, the AI will be asked
        to rewrite the provided message according to the instruction.
        """
        # We need to find the message our response was directed at
        raw_target_message = None
        # If our response is a reply, get the referenced message
        if (
            raw_message.reference
            and isinstance(raw_message.reference.resolved, discord.Message)
            and not self.decide_to_respond.is_hidden_message(
                raw_message.reference.resolved.content
            )
            and not await self._is_hidden_by_reaction(
                raw_message.reference.resolved
                if raw_message.reference.resolved.reactions
                else await response_channel.fetch_message(
                    raw_message.reference.resolved.id
                )
            )
        ):
            raw_target_message = raw_message.reference.resolved
            target_message = discord_utils.discord_message_to_generic_message(
                raw_target_message
            )
        else:
            # otherwise, try to get the latest message before the provided raw message
            # that isn't hidden
            async for raw_msg in response_channel.history(
                limit=self.prompt_generator.history_messages,
                before=raw_message
            ):
                if (
                    self.decide_to_respond.is_hidden_message(raw_msg.content)
                    or await self._is_hidden_by_reaction(raw_msg)
                ):
                    continue
                raw_target_message = raw_msg
                target_message = discord_utils.discord_message_to_generic_message(
                    raw_target_message
                )
                break
        if not raw_target_message or not target_message:
            raise discord.DiscordException(
                "Could not find the message this message was in response to."
            )

        # Now that we know the last user message, begin generating a new response
        target_message.attachments += await self._get_image_descriptions(
            raw_target_message
        )

        await self._send_text_response_in_channel(
            message=target_message,
            raw_message=raw_target_message,
            is_summon_in_public_channel=False,
            response_channel=response_channel,
            existing_message=raw_message,
            rewrite_request=rewrite_request
        )

    async def _render_response(
        self,
        response: typing.Union[typing.AsyncIterator[str], str],
        response_stat: response_stats.ResponseStats,
        response_channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ] = None,
        existing_message: typing.Optional[discord.Message] = None
    ) -> typing.Tuple[int, bool]:
        """
        Determines if we're streaming the response live, splitting the response
        into individual messages and posting them one-by-one, or posting the
        entire response at once. It then calls the appropriate method to render
        the response to the channel accordingly. If an existing message is
        provided, its contents are replaced with the response.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we need to abort the response entirely
        """
        # If we're streaming the response live, call the relevant method
        if self.stream_responses:
            (
                sent_message_count, abort_response
            ) = await self._render_streaming_response(
                response, # type: ignore
                response_stat,
                response_channel,
                reference,
                existing_message
            )
        # Send the response sentence by sentence in a new message
        # each time, notifying the channel.
        elif self.split_responses:
            async for sentence in response: # type: ignore
                (
                    sent_message_count, abort_response
                ) = await self._send_messages(
                        sentence,
                        response_stat,
                        response_channel,
                        reference,
                        existing_message
                    )
        # or finally, post the whole message at once
        else:
            (
                sent_message_count, abort_response
            ) = await self._send_messages(
                    response, # type: ignore
                    response_stat,
                    response_channel,
                    reference,
                    existing_message
                )

        return sent_message_count, abort_response

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
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ] = None,
        existing_message: typing.Optional[discord.Message] = None
    ) -> typing.Tuple[int, bool]:
        """
        Renders a streaming response into a message by editing it with updated
        contents each time a new group of response tokens is received. If the
        size of the response exceeds the character limit, the response is
        continued in a new message.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we aborted the response
        """
        buffer = ""
        response = ""
        last_message = existing_message
        last_message_time = 0
        message_to_log = None
        sent_message_count = 0
        abort_response = False

        async for tokens in response_iterator:
            if self.stream_responses == "token":
                if not tokens:
                    continue
                buffer, abort_response = self.immersion_breaking_filter.filter(buffer + tokens)
                # If we would exceed the character limit, post what we have and start a new message
                if len(buffer.strip()) > self.message_character_limit:
                    if existing_message:
                        # If we're editing an existing message, truncate excess and
                        # abort. We can't send multiple responses in past history.
                        fancy_logger.get().debug(
                            "Response exceeded %d character limit! Truncating excess.",
                            self.message_character_limit
                        )
                        break
                    fancy_logger.get().debug(
                        "Response exceeded %d character limit! Posting current "
                        + "message and continuing in a new message.",
                        self.message_character_limit
                    )
                    buffer = ""
                    response = ""
                    reference = last_message
                    last_message = None
                response, abort_response = self.immersion_breaking_filter.filter(response + tokens)

            elif self.stream_responses == "sentence":
                sentence, abort_response = self.immersion_breaking_filter.filter(tokens)
                if not sentence:
                    continue
                sentence = sentence.rstrip(" ") + " "
                # If we would exceed the character limit, start a new message
                if len((response + sentence).strip()) > self.message_character_limit:
                    if existing_message:
                        fancy_logger.get().debug(
                            "Response exceeded %d character limit! Truncating excess.",
                            self.message_character_limit
                        )
                        break
                    fancy_logger.get().debug(
                        "Response exceeded %d character limit! Posting current "
                        + "message and continuing in a new message.",
                        self.message_character_limit
                    )
                    response = ""
                    reference = last_message
                    last_message = None
                response += sentence
                # The sentence iterator does not group by time, therefore we ensure that
                # we wait for at least a rate-limit interval before continuing.
                now = time.perf_counter()
                if now < last_message_time + self.stream_responses_speed_limit:
                    await asyncio.sleep(
                        (last_message_time + self.stream_responses_speed_limit) - now
                    )
                last_message_time = time.perf_counter()

            # don't send an empty message
            if not response.strip():
                continue

            # if we are aborting a response, we want to at least post
            # the valid parts, so don't abort quite yet.
            if not last_message:
                # Reference cannot be None, so we handle it gracefully
                kwargs = {}
                if reference:
                    kwargs["reference"] = reference
                last_message = await response_channel.send(
                    response.strip(),
                    allowed_mentions=self._allowed_mentions,
                    suppress_embeds=True,
                    **kwargs
                )
                sent_message_count += 1
            else:
                last_message = await last_message.edit(
                    content=response.strip(),
                    allowed_mentions=self._allowed_mentions,
                    suppress=True,
                )
                # If we never sent an initial message (e.g. we're editing an existing
                # one), increment the counter since we did actually send a response.
                if not sent_message_count:
                    sent_message_count += 1

            # Only log the first sent message with the repetition tracker
            if sent_message_count == 1:
                message_to_log = last_message

            # we want to abort the response only after we've sent any valid
            # messages, and potentially removed any partial immersion-breaking
            # lines that we posted when they were in the process of being received.
            if abort_response:
                break

            response_stat.log_response_part()

        if message_to_log:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(message_to_log)
            )

        return sent_message_count, abort_response

    async def _send_messages(
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
        reference: typing.Optional[
            typing.Union[discord.Message, discord.MessageReference]
        ] = None,
        existing_message: typing.Optional[discord.Message] = None
    ) -> typing.Tuple[int, bool]:
        """
        Given a string that represents an individual response message,
        post it as a message in the given channel. If the response is
        too large to fit in a single message, split it into as many
        messages as required. If an existing message is provided,
        The response is truncated to fit in one message and the
        contents of the provided message is replaced with the new
        response.

        It also looks to see if a message contains a termination string,
        and if so it will return False to indicate that we should stop
        the response.

        Also does some bookkeeping to make sure we don't repeat ourselves,
        and to track how many messages we've sent.

        Returns a tuple with:
        - the number of sent Discord messages
        - a boolean indicating if we need to abort the response entirely
        """
        response, abort_response = self.immersion_breaking_filter.filter(response)
        sent_message_count = 0
        last_message = existing_message
        last_message_time = 0
        message_to_log = None
        kwargs = {}
        if reference:
            kwargs["reference"] = reference

        # Hopefully we don't get here often but if we do, split the response
        # into sentences, append them to a response buffer until the next
        # sentence would cause the response to exceed the character limit,
        # then post what we have and continue in a new message.
        if len(response.strip()) > self.message_character_limit and not existing_message:
            new_response = ""
            # Split lines using the compiled regex from the immersion-breaking filter,
            # which uses regex split with a capturing group to return the split
            # character(s) in the list.
            for line in self.immersion_breaking_filter.split(response):
                if not line.strip(self.immersion_breaking_filter.line_split_pattern):
                    new_response += line
                    continue
                for sentence in self.immersion_breaking_filter.segment(line):
                    if len((new_response + sentence).strip()) > self.message_character_limit:
                        fancy_logger.get().warning(
                            "Response exceeded %d character limit by %d "
                            + "characters! Posting current message and continuing "
                            + "in a new message.",
                            self.message_character_limit,
                            len(response) - self.message_character_limit
                        )
                        last_message = await response_channel.send(
                            new_response.strip(),
                            allowed_mentions=self._allowed_mentions,
                            suppress_embeds=True,
                            **kwargs
                        )
                        sent_message_count += 1
                        response_stat.log_response_part()
                        # If we are splitting a large message, use only the first message
                        # we send for the repetition tracker.
                        if not message_to_log:
                            message_to_log = last_message
                        # Reply to our last message in a chain that tracks the whole response
                        kwargs["reference"] = last_message
                        last_message = None
                        new_response = ""
                    new_response += sentence
            response = new_response
            # Finally, wait for the configured rate-limit timeout
            now = time.perf_counter()
            if now < last_message_time + self.stream_responses_speed_limit:
                await asyncio.sleep(
                    (last_message_time + self.stream_responses_speed_limit) - now
                )
            last_message_time = time.perf_counter()

        # We can't send an empty message
        response = response.strip()
        if response:
            # If our response is too big, just truncate it. There's no easy way
            # to send multiple messages in order through past chat history.
            if len(response) > self.message_character_limit:
                response = response[:self.message_character_limit]
            # If we haven't passed an existing message, send a new one
            if not last_message:
                kwargs = {}
                if reference:
                    kwargs["reference"] = reference
                last_message = await response_channel.send(
                    response,
                    allowed_mentions=self._allowed_mentions,
                    suppress_embeds=True,
                    **kwargs
                )
                sent_message_count += 1
            # otherwise, edit the existing message
            else:
                last_message = await last_message.edit(
                    content=response,
                    allowed_mentions=self._allowed_mentions,
                    suppress=True
                )
                if not sent_message_count:
                    sent_message_count += 1
            response_stat.log_response_part()

            if not message_to_log:
                message_to_log = last_message

        # Log the message with the repetition tracker, if we sent one
        if message_to_log:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(message_to_log)
            )

        return sent_message_count, abort_response

    async def _filter_history_message(
      self,
      message: discord.Message,
      stop_before_message_id: typing.Optional[int] = None,
   ) -> typing.Tuple[typing.Optional[types.GenericMessage], bool]:
        """
        Filter out any messages that we don't want to include in the
        AI's history.

        These include:
        - messages generated by our image generator
        - messages at or before the stop_before_message_id
        - messages that have been explicitly hidden by the user
        - system messages that are not default messages or replies

        Also, modify the message in the following ways:
        - if the message is from the AI, set the author name to
        the AI's persona name, not its Discord account name
        - remove <@_0000000_> user ID-based message mention text,
        replacing them with @username mentions
        - remove <#_0000000_> channel ID-based message mention text,
        replacing them with #channel mentions
        - remove <:emoji_name:_0000000_> emoji IDs, replacing them
        with the :emoji_name: between colons.
        """
        # If we've hit the throttle message, stop and don't add any more history
        if stop_before_message_id and message.id == stop_before_message_id:
            generic_message = discord_utils.discord_message_to_generic_message(message)
            return generic_message, False

        # Don't include system messages
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply
        ):
            return None, True

        # Don't include hidden messages
        if (
            self.decide_to_respond.is_hidden_message(message.content)
            or await self._is_hidden_by_reaction(message)
        ):
            return None, True

        generic_message = discord_utils.discord_message_to_generic_message(message)

        if generic_message.author_id == self.bot_user_id:
            # hack: use the suppress_embeds=True flag to indicate that this message
            # is one we generated as part of a text response, as opposed to an
            # image or application message
            if not message.flags.suppress_embeds:
                # Substitute any images we generated for the alt text description
                alt_text = []
                for attachment in message.attachments:
                    if (
                        attachment.content_type
                        and "image/" in attachment.content_type
                        and attachment.description
                    ):
                        image_prompt = re.match(
                            r".*'(.*)'$", attachment.description
                        )
                        if not image_prompt:
                            continue
                        image_prompt = self.template_store.format(
                            templates.Templates.PROMPT_IMAGE_SENT,
                            {
                                templates.TemplateToken.AI_NAME: self.persona.ai_name,
                                templates.TemplateToken.IMAGE_PROMPT: image_prompt.group(1)
                            }
                        )
                        alt_text.append(image_prompt)
                if not alt_text:
                    return None, True
                generic_message.body_text = "\n".join(alt_text)

            # Make sure the AI always sees its persona name in the transcript, even
            # if the chat program has it under a different account name.
            generic_message.author_name = self.persona.ai_name

        # Replace Discord-specific codes with the human (or AI) readable content
        if isinstance(message.channel, (discord.abc.GuildChannel, discord.Thread)):
            fn_user_id_to_name = discord_utils.guild_user_id_to_name(
                message.channel.guild
            )
            await discord_utils.replace_channel_mention_ids_with_names(
                self,
                generic_message
            )
        elif isinstance(message.channel, discord.GroupChannel):
            fn_user_id_to_name = discord_utils.group_user_id_to_name(
                message.channel,
            )
        else:
            # This is a DM or other channel type
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

    async def _is_hidden_by_reaction(self, raw_message: discord.Message) -> bool:
        """
        Takes a Discord Message and checks if it has any of the configured
        ignore reactions on it. If any ignore reactions are present and are
        either on messages from the AI, or the user's own messages, the
        method returns True, otherwise False.
        """
        if not self.ignore_reactions:
            return False

        for reaction in raw_message.reactions:
            if reaction.is_custom_emoji():
                emoji: str = reaction.emoji.name # type: ignore
            else:
                emoji: str = reaction.emoji # type: ignore
            if emoji in self.ignore_reactions:
                if (
                    # The reaction was on a message from the AI
                    reaction.message.author.id == self.bot_user_id
                    # or a message from any bot (for PluralKit users, etc)
                    or raw_message.author.bot
                ):
                    return True
                async for reactor in reaction.users():
                    # If a user who reacted to this message is the author,
                    # filter it.
                    if reactor.id == reaction.message.author.id:
                        return True
        return False

    async def _filtered_history_iterator(
        self,
        message: types.GenericMessage,
        channel: typing.Union[
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel,
            discord.DMChannel,
            discord.GroupChannel
        ],
        stop_before_message_id: typing.Optional[int],
        ignore_all_until_message_id: typing.Optional[int],
        limit: int
    ) -> typing.AsyncIterator[types.GenericMessage]:
        """
        Gathers channel history up to the limit and returns an asynchronous
        iterator of all the messages the AI should see. If any messages are
        filtered out, this recursively fetches history (ignoring the limit)
        until all filtered messages are accounted for, we reach our message
        limit, or we reach the beginning of the channel.

        When returning the history of a thread that was started as a reply
        to an existing message, Discord does not include the message that
        kicked off the thread. It will show it in the UI as if it were,
        but it's not one of the messages returned by the history iterator.
        This method attempts to return that message as well, if we need it.
        """
        messages = 0
        filtered_messages = 0
        messages_fetched = 0
        last_returned = None
        ignoring_all = bool(ignore_all_until_message_id)

        channel_history = channel.history(limit=limit)
        async for raw_message in channel_history:
            # Stop if we've collected as many messages as we're looking for
            if messages >= limit:
                return
            # Track the number of messages we've pulled out of the iterator
            messages_fetched += 1

            if ignoring_all:
                if raw_message.id == ignore_all_until_message_id:
                    ignoring_all = False
                else:
                    # This message was sent after the message we're
                    # responding to, so filter out it as to not confuse
                    # the AI into responding to content from that message
                    # instead.
                    continue

            # We ignore the message matching the ID of the provided GenericMessage
            # and yield the provided message directly, as we may have added
            # attachments to the message previously.
            if raw_message.id == message.message_id:
                yield message
                continue

            last_returned = raw_message
            sanitized_message, allow_more = await self._filter_history_message(
                raw_message,
                stop_before_message_id=stop_before_message_id
            )
            if sanitized_message:
                yield sanitized_message
                messages += 1
            else:
                filtered_messages += 1
            if not allow_more:
                # We've hit a message which requires us to stop fetching history
                return

        # If we filtered any messages, fetch additional messages, recursively
        # fetching new channel history as required, until all filtered messages
        # have been made up for, we reach our target message limit, or we reach
        # the beginning of the channel.
        if self.prompt_generator.automatic_lookback:
            while filtered_messages:
                if messages >= limit:
                    return

                # Try to pull the next message out of the iterator
                raw_message = await anext(channel_history, None)
                messages_fetched += 1

                if not raw_message:
                    # If we exhausted the iterator but didn't reach the limit,
                    # this is the beginning of the channel
                    if messages_fetched < limit:
                        break
                    # Fetch another iterator if the channel still has history,
                    # starting before the last returned message
                    fancy_logger.get().debug(
                        "Reached the history limit but collected only %d messages. "
                        + "Fetching more history...",
                        messages
                    )
                    messages_to_fetch = max(limit, filtered_messages)
                    messages_fetched = 0

                    channel_history = channel.history(
                        limit=messages_to_fetch,
                        before=last_returned
                    )
                    continue

                # otherwise, continue to filter or yield the message
                last_returned = raw_message
                sanitized_message, _ = await self._filter_history_message(
                    raw_message
                )
                if sanitized_message:
                    yield sanitized_message
                    messages += 1
                    filtered_messages -= 1

        # We've reached the beginning of the history, but still have space.
        if last_returned and messages < limit:
            reference = None
            # If this message was a reply to another message,
            # return that message
            if (
                last_returned.type is discord.MessageType.reply
                and last_returned.reference
            ):
                reference = last_returned.reference.resolved
            # otherwise, if this is a thread in a text channel, return the
            # message that started it. It's impossible for the start of a
            # thread to be a reply, so we do this after checking for that.
            elif (
                isinstance(channel, discord.Thread)
                and isinstance(channel.parent, discord.TextChannel)
            ):
                # This will be either a default message, or a thread_created
                # system message, which will be filtered out.
                if channel.starter_message:
                    reference = channel.starter_message
                else:
                    try:
                        reference = await channel.parent.fetch_message(channel.id)
                    except discord.NotFound:
                        pass

            # The resolved message may be None or a DeletedReferencedMessage
            # if the message was deleted.
            if isinstance(reference, discord.Message):
                sanitized_message, _ = await self._filter_history_message(
                    reference
                )
                if sanitized_message:
                    yield sanitized_message
