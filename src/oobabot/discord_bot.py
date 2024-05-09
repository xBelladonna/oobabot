# -*- coding: utf-8 -*-
"""
Main bot class.  Contains Discord-specific code that can't
be easily extracted into a cross-platform library.
"""

import asyncio
import typing
import io
import re
from collections import deque
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


class DiscordBot(discord.Client):
    """
    Main bot class.  Connects to Discord, monitors for messages,
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

        self.ai_user_id = -1
        self.message_character_limit = 2000

        self.dont_split_responses = discord_settings["dont_split_responses"]
        self.ignore_dms = discord_settings["ignore_dms"]
        self.ignore_prefixes = discord_settings["ignore_prefixes"]
        self.message_accumulation_period = discord_settings["message_accumulation_period"]
        self._allowed_mentions = discord_settings["allowed_mentions"]
        for allowed_mention_type in self._allowed_mentions:
            if allowed_mention_type not in ["everyone", "users", "roles"]:
                raise ValueError(
                    f"Unrecognised allowed mention type '{allowed_mention_type}'. "
                    + "Please fix your configuration."
                )
        # build allowed mentions object from configuration
        self._allowed_mentions = discord.AllowedMentions(
            everyone="everyone" in self._allowed_mentions,
            users="users" in self._allowed_mentions,
            roles="roles" in self._allowed_mentions,
        )
        self.reply_in_thread = discord_settings["reply_in_thread"]
        self.stop_markers = discord_settings["stop_markers"]
        self.prevent_impersonation = discord_settings["prevent_impersonation"]
        if self.prevent_impersonation not in ["standard", "aggressive", "comprehensive"]:
            raise ValueError(
                f"Unknown value '{self.prevent_impersonation}' for `prevent_impersonation`. "
                + "Please fix your configuration"
            )
        self.stream_responses = discord_settings["stream_responses"]
        if self.stream_responses and self.stream_responses not in ["token", "sentence"]:
            raise ValueError(
                f"Unknown value '{self.stream_responses}' for `stream_responses`. "
                + "Please fix your configuration."
            )
        self.stream_responses_speed_limit = discord_settings["stream_responses_speed_limit"]

        # add stopping_strings to stop_markers
        self.stop_markers.extend(self.ooba_client.get_stopping_strings())

        # Identify our intents with the Gateway
        super().__init__(intents=discord_utils.get_intents())

        # Instantiate double-ended message queue
        self.message_queue = deque()
        # Get a sentence segmenter ready
        self.sentence_splitter = pysbd.Segmenter(language="en", clean=False, char_span=True)

    async def on_ready(self) -> None:
        guilds = self.guilds
        num_guilds = len(guilds)
        num_channels = sum(len(guild.channels) for guild in guilds)

        if self.user:
            self.ai_user_id = self.user.id
            user_id_str = self.user.name
        else:
            user_id_str = "<unknown>"

        fancy_logger.get().info(
            "Connected to discord as %s (ID: %d)", user_id_str, self.ai_user_id
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
            fancy_logger.get().debug("Response Grouping: returned as single messages")
        else:
            fancy_logger.get().debug(
                "Response Grouping: split into messages by sentence"
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

        # log unsolicited_channel_cap
        cap = self.decide_to_respond.get_unsolicited_channel_cap()
        cap = str(cap) if cap > 0 else "<unlimited>"
        fancy_logger.get().debug(
            "Unsolicited channel cap: %s",
            cap,
        )

        str_wakewords = (
            ", ".join(self.persona.wakewords) if self.persona.wakewords else "<none>"
        )
        fancy_logger.get().debug("Wakewords: %s", str_wakewords)

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
                "The bot is not connected to any servers.  "
                + "Please add the bot to a server here:",
            )
            fancy_logger.get().warning(
                discord_utils.generate_invite_url(self.ai_user_id)
            )

    async def on_message(self, raw_message: discord.Message) -> None:
        """
        Called when a message is received from Discord.

        This method is called for every message that the bot can see.
        It decides whether to respond to the message, and if so,
        queues the message for processing.

        :param raw_message: The raw message from Discord.
        """

        # If the message is not a command, proceed with regular message handling
        try:
            # Add the message to the queue
            self.message_queue.appendleft(raw_message)
            # Start processing the message queue for the first message received
            if len(self.message_queue) == 1:
                self.loop.create_task(
                    self.process_message_queue(raw_message.channel, self.message_queue)
                )

        except discord.DiscordException as err:
            fancy_logger.get().error(
                "Error while queueing message for processing: %s", err, exc_info=True
            )

    async def on_message_delete(self, raw_message: discord.Message) -> None:
        """
        Called when a message is deleted from Discord.

        This method is called for every message in the cache that is deleted,
        checks if that message is in our message queue, and removes it if so.
        """
        if raw_message in self.message_queue:
            self.message_queue.remove(raw_message)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        channel = await self.fetch_channel(payload.channel_id)
        try:
            raw_message = await channel.fetch_message(payload.message_id)
        # Sometimes the message is already deleted before we can process it, e.g.
        # the PluralKit bot uses ❌ to delete messages too, so we account for that.
        except discord.NotFound:
            return

        # hide all chat history at and before this message
        if payload.emoji.name == "⏪":
            fancy_logger.get().debug(
                "Received request from %s to hide chat history in %s.",
                raw_message.author.name,
                discord_utils.get_channel_name(channel),
            )

            finished = False
            async for msg in channel.history(limit=self.prompt_generator.history_lines):
                if finished:
                    self.repetition_tracker.hide_messages_before(
                        channel_id=channel.id,
                        message_id=msg.id,
                    )
                    break
                if msg.id == payload.message_id:
                    finished = True
            try:
                await raw_message.clear_reaction(payload.emoji)
            except (discord.Forbidden, discord.NotFound):
                # We can't remove reactions on other users' messages in DMs or Group DMs.
                # Also give up if the reaction isn't there anymore (i.e. someone removed
                # it before we could).
                pass
            return

        # only process the below reactions if it was to one of our messages
        if raw_message.author.id != self.ai_user_id:
            return

        # message deletion
        if payload.emoji.name == "❌":
            fancy_logger.get().debug(
                "Received message deletion request from %s in %s. Deleting message...",
                raw_message.author.name,
                discord_utils.get_channel_name(channel),
            )
            try:
                await raw_message.delete()
            except discord.NotFound:
                # The message was somehow deleted already, give up.
                fancy_logger.get().debug("Message is already gone! Giving up.")
            return

        # message regeneration
        if payload.emoji.name == "🔁":
            message = discord_utils.discord_message_to_generic_message(raw_message)
            fancy_logger.get().debug(
                "Received message regeneration request from %s. Regenerating message...",
                raw_message.author.name,
            )
            try:
                async with channel.typing():
                    await self._regenerate_message(message, raw_message, channel)
                if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
                    return
                try:
                    await raw_message.clear_reaction(payload.emoji)
                except (discord.Forbidden, discord.NotFound):
                    pass
            except discord.DiscordException as err:
                fancy_logger.get().error("Error while processing reaction: %s", err, exc_info=True)
                self.response_stats.log_response_failure()

    async def process_message_queue(
            self,
            channel: typing.Union[
                discord.abc.GuildChannel,
                discord.Thread,
                discord.DMChannel,
                discord.GroupChannel,
            ],
            message_queue: typing.Deque[typing.Tuple[discord.Message]]
        ) -> None:
        """
        Pops the latest message from the queue and handles a response to it.

        The current implementation simply processes the latest message and discards
        the rest, but leaves room for more complex logic in the future, such as
        selectively handling messages in the order they were received.
        """
        # Wait if we're accumulating messages. We avoid this in DMs or Group DMs
        # rather arbitrarily, as the feature was initially designed for bots like
        # PluralKit and Tupperbox that rapidly delete and re-post user messages
        # under different names, and they can't be present in these channel types.
        if (
            self.message_accumulation_period
            and not self.decide_to_respond.guaranteed_response
            and not isinstance(channel, (discord.DMChannel, discord.GroupChannel))
        ):
            await asyncio.sleep(self.message_accumulation_period)
        # If the queue is empty, abort response
        if not message_queue:
            return

        # otherwise, process the latest message in our queue
        raw_message = message_queue.popleft()

        message = discord_utils.discord_message_to_generic_message(raw_message)
        should_respond, is_summon = self.decide_to_respond.should_reply_to_message(
            self.ai_user_id, message
        )
        # Did we guarantee a response? If so, take note of the state and immediately
        # reset the flag. This is crucial to remember to do otherwise we will get into
        # an infinite recursive loop of responding to ourselves.
        guaranteed_response = self.decide_to_respond.guaranteed_response
        if guaranteed_response:
            self.decide_to_respond.guaranteed_response = False
        if not should_respond:
            return
        is_summon_in_public_channel = is_summon and isinstance(
            message, types.ChannelMessage
        )

        # Sometimes it's the case where the message we got has already been deleted.
        # We attempt to prevent this as much as possible, but it might still happen.
        # This attempts to catch it and grab the latest message to reply to anyway.
        try:
            await channel.fetch_message(message.message_id)
        except discord.NotFound:
            # First check the message queue again, in case we only just missed one. This
            # time we pop from the right, to get the message sent right after the deleted
            # one, in case a new message has actually come in since we last checked.
            if message_queue:
                raw_message = message_queue.pop()
                message = discord_utils.discord_message_to_generic_message(raw_message)
            else:
                # otherwise just get the latest visible message from the channel
                skip = False
                async for msg in channel.history(limit=self.prompt_generator.history_lines):
                    for ignore_prefix in self.ignore_prefixes:
                        if msg.content.startswith(ignore_prefix):
                            skip = True
                            break
                        skip = False
                    if skip:
                        continue
                    raw_message = msg
                    break
                message = discord_utils.discord_message_to_generic_message(raw_message)

        # Clear the queue once we have our latest message, we don't need it anymore
        message_queue.clear()

        # If the message is hidden, abort response. We do this here instead of in
        # decide_to_respond, in case the user is using something like PluralKit or
        # Tupperbox and the original message (which was deleted) began with a different
        # sequence. Because we wait to accumulate messages, this ensures the deleted
        # message doesn't trigger a response to whatever the latest message ends up being.
        if not guaranteed_response:
            for ignore_prefix in self.ignore_prefixes:
                if message.body_text.startswith(ignore_prefix):
                    fancy_logger.get().debug("Message is hidden, aborting response.")
                    return

        try:
            async with channel.typing():
                await self._handle_response(
                    message,
                    raw_message,
                    is_summon_in_public_channel,
                )
        except discord.DiscordException as err:
            fancy_logger.get().error(
                "Error while processing message: %s", err, exc_info=True
            )

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
        if self.image_generator:
            # are we creating an image?
            image_prompt = self.image_generator.maybe_get_image_prompt(message.body_text)

        # Determine if there are images and get descriptions (if Vision is enabled)
        images = []
        image_descriptions = []
        if self.vision_client:
            if self.vision_client.fetch_urls:
                urls = self.vision_client.url_extractor.findall(message.body_text)
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
                                "Error pre-processing image: %s", e, exc_info=True
                            )
            for image in images:
                try:
                    fancy_logger.get().debug("Getting image description...")
                    description = await self.vision_client.get_image_description(image)
                    if description:
                        image_descriptions.append(description)
                except Exception as e:
                    fancy_logger.get().error("Error processing image: %s", e, exc_info=True)

        result = await self._send_text_response(
            message=message,
            raw_message=raw_message,
            image_descriptions=image_descriptions,
            image_requested=image_prompt is not None,
            is_summon_in_public_channel=is_summon_in_public_channel,
        )
        if not result:
            # we failed to create a thread that the user could
            # read our response in, so we're done here.  Abort!
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
        if self.image_generator and image_prompt:
            image_task = self.image_generator.generate_image(
                image_prompt,
                message,
                raw_message,
                response_channel=response_channel,
            )

        response_tasks = [
            task for task in [message_task, image_task] if task
        ]

        # Use asyncio.gather instead of asyncio.wait to properly handle exceptions
        if response_tasks:
            done, _pending = await asyncio.wait(response_tasks, return_when=asyncio.ALL_COMPLETED)
            # Check for exceptions in the tasks that have completed
            for task in done:
                if task.exception():
                    fancy_logger.get().error(
                        "Exception while running %s. Response: %s",
                        task.get_coro(),
                        task.exception(),
                        stack_info=True,
                    )
                    raise task.exception()

    async def _generate_response(
        self,
        message: types.GenericMessage,
        recent_messages: typing.AsyncIterator,
        image_descriptions: typing.List[str],
        image_requested: bool,
        response_channel: discord.abc.Messageable,
        as_string: bool = False,
    ) -> typing.Union[typing.AsyncIterator, str]:
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
            skip = False
            for msg in recent_messages_list:
                for ignore_prefix in self.ignore_prefixes:
                    if message.body_text.startswith(ignore_prefix):
                        skip = True
                        break
                    skip = False
                if not skip and msg.author_id == message.author_id:
                    # Append the image descriptions to the body text of the user's last message
                    msg.body_text += "\n" + description_text
                    break
        # Convert the list back into an async generator
        async def _list_to_async_gen(lst: typing.List) -> typing.AsyncGenerator:
            for item in lst:
                yield item
        recent_messages_async_gen = _list_to_async_gen(recent_messages_list)

        # Generate the prompt prefix using the modified recent messages
        if isinstance(response_channel, (discord.abc.GuildChannel, discord.Thread)):
            guild_name = response_channel.guild.name
            response_channel_name = response_channel.name
        elif isinstance(response_channel, discord.GroupChannel):
            guild_name = "Group DM"
            response_channel_name = response_channel.name
        else:
            guild_name = "Direct Message"
            response_channel_name = "None"
        prompt_prefix = await self.prompt_generator.generate(
            ai_user_id=self.ai_user_id,
            message_history=recent_messages_async_gen,
            image_requested=image_requested,
            guild_name=guild_name,
            response_channel=response_channel_name,
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
            recent_members.pop(self.user.display_name, None) # we don't want our own name
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
                ).strip("\n")
            def _get_canonicalized_name(user_name: str) -> str:
                canonicalized_name = emoji.replace_emoji(
                    user_name.split()[0], ""
                ).strip().capitalize()
                return canonicalized_name if len(canonicalized_name) >= 3 else user_name

            # TODO: O(n^2) time complexity... figure out how to make this faster
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
                    stopping_strings.append("\n" + _get_canonicalized_name(user_name))
                elif self.prevent_impersonation == "comprehensive":
                    stopping_strings.append(_get_user_prompt_prefix(user_name))
                    stopping_strings.append("\n" + _get_canonicalized_name(user_name))

        fancy_logger.get().debug("Generating text response...")
        if as_string:
            response = await self.ooba_client.request_as_string(prompt_prefix, stopping_strings)
            return response, response_stat
        if self.stream_responses:
            generator = self.ooba_client.request_as_grouped_tokens(
                prompt_prefix,
                stopping_strings,
                interval=self.stream_responses_speed_limit,
            )
        else:
            generator = self.ooba_client.request_by_message(
                prompt_prefix,
                stopping_strings,
            )

        return generator, response_stat

    async def _send_text_response(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        image_descriptions: typing.List[str],
        image_requested: bool,
        is_summon_in_public_channel: bool,
    ) -> typing.Optional[typing.Tuple[asyncio.Task, discord.abc.Messageable]]:
        """
        Send a text response to a message.

        This method determines what channel or thread to post the message
        in, creating a thread if necessary.  It then posts the message
        by calling _send_text_response_to_channel().

        Returns a tuple of the task that was created to send the message,
        and the channel that the message was sent to.

        If no message was sent, the task and channel will be None.
        """
        response_channel = raw_message.channel
        if (
            self.reply_in_thread
            and isinstance(raw_message.channel, discord.TextChannel)
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
                # message.  We'd end up creating a thread for that
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
            response_channel=response_channel,
        )
        response_task = asyncio.create_task(response_coro)
        return (response_task, response_channel)

    async def _send_text_response_in_channel(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        image_descriptions: typing.List[str],
        image_requested: bool,
        is_summon_in_public_channel: bool,
        response_channel: discord.abc.Messageable,
    ) -> None:
        """
        Getting closer now! This method takes the generator from
        _generate_response() and then calls _send_response_message()
        to send each message.
        """

        repeated_id = self.repetition_tracker.get_throttle_message_id(
            response_channel.id
        )

        # determine if we're responding to a specific message that
        # summoned us.  If so, find out what message ID that was, so
        # that we can ignore all messages sent after it (as not to
        # confuse the AI about what to reply to)
        reference = None
        # ignore_all_until_message_id = None
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
        # will return a string or generator based on configuration
        response, response_stat = await self._generate_response(
            message=message,
            recent_messages=recent_messages,
            image_descriptions=image_descriptions,
            image_requested=image_requested,
            response_channel=response_channel,
            as_string=self.dont_split_responses and not self.stream_responses,
        )

        try:
            if self.stream_responses == "token":
                last_sent_message = await self._render_streaming_response(
                    response,
                    response_stat,
                    response_channel,
                    self._allowed_mentions,
                    reference,
                )
                if last_sent_message:
                    sent_message_count = 1
            elif self.stream_responses == "sentence":
                last_sent_message = await self._render_response_by_sentence(
                    response,
                    response_stat,
                    response_channel,
                    self._allowed_mentions,
                    reference,
                )
                if last_sent_message:
                    sent_message_count = 1
            else:
                # post the whole message at once
                if self.dont_split_responses:
                    if len(response) > self.message_character_limit:
                        # hopefully we don't get here often but if we do, split the response
                        # into sentences, and append them to our response until the next
                        # sentence would cause the response to exceed the character limit.
                        new_response = ""
                        sentences: typing.List[
                            pysbd.utils.TextSpan
                        ] = [x.sent for x in self.sentence_splitter.segment(response)]
                        for sentence in sentences:
                            if len(new_response) > self.message_character_limit:
                                break
                            new_response += sentence.sent
                        response = new_response
                    (
                        last_sent_message,
                        aborted_by_us,
                    ) = await self._send_response_message(
                        response,
                        response_stat,
                        response_channel,
                        self._allowed_mentions,
                        reference,
                    )
                    if last_sent_message:
                        sent_message_count = 1
                # or finally, send the response sentence by sentence
                # in a new message each time, notifying the channel.
                else:
                    last_sent_message = None
                    async for sentence in response:
                        if len(sentence) > self.message_character_limit:
                            # idk how the hell we might get here but best to consider it
                            sentence = sentence[:self.message_character_limit]
                        (
                            sent_message,
                            abort_response,
                        ) = await self._send_response_message(
                            sentence,
                            response_stat,
                            response_channel,
                            self._allowed_mentions,
                            reference,
                        )
                        if sent_message:
                            last_sent_message = sent_message
                            sent_message_count += 1
                            # only use the reference for the first
                            # message in a multi-message chain
                            reference = None
                        if abort_response:
                            aborted_by_us = True
                            break

        except discord.DiscordException as err:
            fancy_logger.get().error("Error while sending message: %s", err, exc_info=True)
            self.response_stats.log_response_failure()
            return

        if 0 == sent_message_count:
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
        response_channel: discord.abc.Messageable,
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[discord.MessageReference],
    ) -> typing.Tuple[typing.Optional[discord.Message], bool]:
        """
        Given a string that represents an individual response message,
        post it in the given channel.

        It also looks to see if a message contains a termination string,
        and if so it will return False to indicate that we should stop
        the response.

        Also does some bookkeeping to make sure we don't repeat ourselves,
        and to track how many messages we've sent.

        Returns a tuple with:
        - the sent discord message, if any
        - a boolean indicating if we need to abort the response entirely
        """
        (sentence, abort_response) = self._filter_immersion_breaking_lines(response)
        if abort_response:
            return (None, True)
        if not sentence:
            # we can't send an empty message
            return (None, False)

        response_message = await response_channel.send(
            sentence,
            allowed_mentions=allowed_mentions,
            suppress_embeds=True,
            reference=reference,  # type: ignore
        )
        self.repetition_tracker.log_message(
            response_channel.id,
            discord_utils.discord_message_to_generic_message(response_message),
        )

        response_stat.log_response_part()
        return (response_message, False)

    async def _regenerate_message(
        self,
        message: types.GenericMessage,
        raw_message: discord.Message,
        channel: typing.Union[
            discord.abc.GuildChannel,
            discord.DMChannel,
            discord.GroupChannel,
            discord.Thread,
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
        response, response_stat = await self._generate_response(
            message=message,
            recent_messages=recent_messages,
            image_descriptions=[],
            image_requested=False,
            response_channel=channel,
            as_string=self.dont_split_responses and not self.stream_responses,
        )

        if self.stream_responses == "token":
            await self._render_streaming_response(
                response,
                response_stat,
                channel,
                self._allowed_mentions,
                existing_message=raw_message,
            )
        elif self.stream_responses == "sentence":
            await self._render_response_by_sentence(
                response,
                response_stat,
                channel,
                self._allowed_mentions,
                existing_message=raw_message,
            )
        else:
            if response:
                await raw_message.edit(content=response)
                response_stat.log_response_part()
            else:
                fancy_logger.get().warning(
                    "An empty response was received from Oobabooga. Please check that "
                    + "the AI is running properly on the Oobabooga server at %s.",
                    self.ooba_client.base_url,
                )
                self.response_stats.log_response_failure()
                return

        self.response_stats.log_response_success(response_stat)
        response_stat.write_to_log(f"Regeneration of message #{message.message_id} done!  ")

    async def _render_streaming_response(
        self,
        response_iterator: typing.AsyncIterator[str],
        response_stat: response_stats.ResponseStats,
        response_channel: discord.abc.Messageable,
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[discord.MessageReference] = None,
        existing_message: typing.Optional[discord.Message] = None,
    ) -> typing.Optional[discord.Message]:
        """
        Renders a streaming response into a message by editing it with updated
        contents each time a new group of response tokens is received.
        """
        response = ""
        last_message = existing_message

        async for token in response_iterator:
            if not token:
                continue
            response += token
            response, abort_response = self._filter_immersion_breaking_lines(response)
            # Abort the stream if our response meets or exceeds Discord's character limit
            if len(response) >= self.message_character_limit:
                break

            # don't send an empty message
            if not response:
                continue

            # if we are aborting a response, we want to at least post
            # the valid parts, so don't abort quite yet.
            if not last_message:
                last_message = await response_channel.send(
                    response,
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    reference=reference,
                )
            else:
                await last_message.edit(
                    content=response,
                    allowed_mentions=allowed_mentions,
                    suppress=True,
                )
                last_message.content = response

            # we want to abort the response only after we've sent any valid
            # messages, and potentially removed any partial immersion-breaking
            # lines that we posted when they were in the process of being received.
            if abort_response:
                break

            response_stat.log_response_part()

        if last_message:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(last_message),
            )

        return last_message

    async def _render_response_by_sentence(
        self,
        response_iterator: typing.AsyncIterator[str],
        response_stat: response_stats.ResponseStats,
        response_channel: discord.abc.Messageable,
        allowed_mentions: discord.AllowedMentions,
        reference: typing.Optional[discord.MessageReference] = None,
        existing_message: typing.Optional[discord.Message] = None,
    ) -> typing.Optional[discord.Message]:
        """
        Renders a streaming response, by sentence, into a message by editing it
        with the updated contents every response streaming interval.
        """
        response = ""
        last_message = existing_message

        async for sentence in response_iterator:
            sentence, abort_response = self._filter_immersion_breaking_lines(sentence)
            if not sentence:
                continue
            response += sentence
            if len(response) >= self.message_character_limit:
                break

            if not last_message:
                last_message = await response_channel.send(
                    response,
                    allowed_mentions=allowed_mentions,
                    suppress_embeds=True,
                    reference=reference,
                )
            else:
                await last_message.edit(
                    content=response,
                    allowed_mentions=allowed_mentions,
                    suppress=True,
                )
                last_message.content = response

            if abort_response:
                break

            response_stat.log_response_part()
            # Wait an interval so we don't hit the rate-limit. This is not done
            # in the ooba_client because the method may be used for other things
            # that don't require waiting.
            await asyncio.sleep(self.stream_responses_speed_limit)

        if last_message:
            self.repetition_tracker.log_message(
                response_channel.id,
                discord_utils.discord_message_to_generic_message(last_message),
            )

        return last_message

    def _filter_immersion_breaking_lines(self, text: str) -> typing.Tuple[str, bool]:
        """
        Given a string that represents an individual response message,
        filter out any lines that would break immersion.

        These include lines that include a termination symbol, lines
        that attempt to carry on the conversation as a different user,
        and lines that include text which is part of the AI prompt.

        Returns the subset of the input string that should be sent,
        and a boolean indicating if we should abort the response entirely,
        ignoring any further lines.
        """
        # First, split the text by 'real' newlines to preserve them
        lines = text.split("\n")
        good_lines = []
        abort_response = False

        for line in lines:
            # Split the line by our pysbd segmenter to get individual sentences
            sentences: typing.List[
                pysbd.utils.TextSpan
            ] = [x.sent for x in self.sentence_splitter.segment(line)]
            good_sentences = []

            for sentence in sentences:
                # if the AI gives itself a second line, just ignore
                # the line instruction and continue
                if self.prompt_generator.bot_prompt_block in sentence:
                    fancy_logger.get().warning(
                        "Filtered out %s from response, continuing.",
                        sentence,
                    )
                    continue

                # hack: abort response if it looks like the AI is
                # continuing the conversation as someone else
                name_identifier = "%%%%%%%%NAME%%%%%%%%"
                username_pattern = self.template_store.format(
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
                username_pattern = re.escape(username_pattern).replace(name_identifier, ".*")
                message_pattern = re.compile(r'^(' + username_pattern + r')\s*(.*)')
                match = message_pattern.match(sentence)
                if match:
                    username_sequence, remaining_text = match.groups()
                    ai_name_prompt = self.template_store.format(
                        templates.Templates.BOT_PROMPT_HISTORY_BLOCK,
                        {
                            templates.TemplateToken.BOT_NAME: self.template_store.format(
                                templates.Templates.BOT_NAME,
                                {
                                    templates.TemplateToken.NAME: self.persona.ai_name,
                                },
                            ),
                            templates.TemplateToken.MESSAGE: "",
                        },
                    ).strip("\n")

                    if username_sequence in ai_name_prompt:
                        # If the username matches the bot's name, trim the username portion
                        # and keep the remaining text
                        fancy_logger.get().warning(
                            "Filtered out '%s' from response, continuing", sentence
                        )
                        sentence = remaining_text  # Trim and keep the rest of the sentence
                    else:
                        # If the username is not the bot's username, abort the response for
                        # breaking immersion
                        fancy_logger.get().warning(
                            "Filtered out '%s' from response, aborting", sentence
                        )
                        abort_response = True
                        break  # Break out of the for-loop processing sentences

                # look for partial stop markers within a sentence
                for marker in self.stop_markers:
                    if marker in sentence:
                        keep_part, removed = sentence.split(marker, 1)
                        fancy_logger.get().warning(
                            "Filtered out '%s' from response, aborting", removed
                        )
                        if keep_part:
                            good_sentences.append(keep_part)
                        abort_response = True
                        break

                if abort_response:
                    break

                # filter out sentences that are entirely made of whitespace/newlines
                if sentence.strip().strip("\n"):
                    good_sentences.append(sentence)

            if abort_response:
                break

            # Join the good sentences and append to good_lines. pysbd preserves the
            # leading/trailing whitespace, so no space in the join is needed.
            good_line = "".join(good_sentences)
            if good_line:
                good_lines.append(good_line)

        return ("\n".join(good_lines), abort_response)

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
        # if we've hit the throttle message, stop and don't add any
        # more history
        if stop_before_message_id and message.id == stop_before_message_id:
            return (None, False)

        generic_message = discord_utils.discord_message_to_generic_message(message)

        if generic_message.author_id == self.ai_user_id:
            # make sure the AI always sees its persona name
            # in the transcript, even if the chat program
            # has it under a different account name
            generic_message.author_name = self.persona.ai_name

            # hack: use the suppress_embeds=True flag to indicate
            # that this message is one we generated as part of a text
            # response, as opposed to an image or application message
            if not message.flags.suppress_embeds:
                # this is a message generated by our image generator
                return (None, True)

        for ignore_prefix in self.ignore_prefixes:
            if generic_message.body_text.startswith(ignore_prefix):
                return (None, True)

        if isinstance(message.channel, discord.DMChannel):
            fn_user_id_to_name = discord_utils.dm_user_id_to_name(
                self.ai_user_id,
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
            # we shouldn't ever end up here... give it a function anyway
            fn_user_id_to_name = discord_utils.dm_user_id_to_name(
                self.ai_user_id,
                self.persona.ai_name,
                message.author.display_name,
            )

        discord_utils.replace_user_mention_ids_with_names(
            generic_message,
            fn_user_id_to_name=fn_user_id_to_name,
        )
        return (generic_message, True)

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
                        # skip one more message to make sure we hide whatever
                        # message we're referring to as well
                        continue
                else:
                    # this message was sent after the message we're
                    # responding to. So filter out it as to not confuse
                    # the AI into responding to content from that message
                    # instead
                    continue

            last_returned = item
            (sanitized_message, allow_more) = await self._filter_history_message(
                item,
                stop_before_message_id=stop_before_message_id,
            )
            if not allow_more:
                # we've hit a message which requires us to stop
                # and look at more history
                return
            if sanitized_message:
                yield sanitized_message
                items += 1

        if last_returned and items < limit:
            # we've reached the beginning of the history, but
            # still have space.  If this message was a reply
            # to another message, return that message as well.
            if not last_returned.reference:
                return

            ref = last_returned.reference.resolved

            # the resolved message may be None if the message
            # was deleted
            if ref and isinstance(ref, discord.Message):
                (sanitized_message, _) = await self._filter_history_message(
                    ref,
                    stop_before_message_id,
                )
                if sanitized_message:
                    yield sanitized_message

    # when looking through the history of a channel, we'll have a goal
    # of retrieving a certain number of lines of history.  However,
    # there are some messages in the history that we'll want to filter
    # out.  These include messages that were generated by our image
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
    # penalty to making this somewhat large.  But still, we want
    # to keep it reasonable.
    MESSAGE_HISTORY_LOOKBACK_BONUS = 20

    async def _recent_messages_following_thread(
        self,
        channel: discord.abc.Messageable,
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
