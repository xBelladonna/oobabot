# -*- coding: utf-8 -*-
"""
Watches the transcript, when a wakeword is detected,
it builds a prompt for the AI, queries it, and
then queues a response.
"""

import asyncio
from collections import deque
import re
import typing

import emoji
import discord

from oobabot import discord_utils
from oobabot import discrivener
from oobabot import fancy_logger
from oobabot import immersion_breaking_filter
from oobabot import ooba_client
from oobabot import prompt_generator
from oobabot import templates
from oobabot import transcript
from oobabot import types


class AudioResponder:
    """
    Watches the transcript, when a wakeword is detected,
    it builds a prompt for the AI, queries it, and
    then queues a response.
    """

    TASK_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        bot_user_id: int,
        channel: discord.VoiceChannel,
        discrivener: discrivener.Discrivener,
        ooba_client: ooba_client.OobaClient,
        prompt_generator: prompt_generator.PromptGenerator,
        template_store: templates.TemplateStore,
        transcript: transcript.Transcript,
        discord_settings: dict
    ):
        self._abort = False
        self._channel = channel
        self._discrivener = discrivener
        self._ooba_client = ooba_client
        self._prompt_generator = prompt_generator
        self._template_store = template_store
        self._transcript = transcript
        self._task: typing.Optional[asyncio.Task] = None
        self._response_queue: typing.Deque[str] = deque()
        self._response_task: typing.Optional[asyncio.Task] = None
        self._first_response = True

        self.bot_user_id: int = bot_user_id
        self.speak_voice_responses: bool = discord_settings["speak_voice_responses"]
        self.post_voice_responses: bool = discord_settings["post_voice_responses"]
        self.prevent_impersonation: str = discord_settings["prevent_impersonation"]
        self.use_immersion_breaking_filter: bool = discord_settings["use_immersion_breaking_filter"]
        # Get our immersion-breaking filter ready
        self.immersion_breaking_filter = immersion_breaking_filter.ImmersionBreakingFilter(
            discord_settings, self._prompt_generator, self._template_store
        )
        flags = re.MULTILINE + re.UNICODE
        # Match dialogue and narration in 2 separate capture groups
        self.dialogue_matcher = re.compile(
            r"\b([\w\d \b\'\.`,;!\?\-]+)|( ?\*.*?(?:\* ?| (?=\")))",
            flags
        )
        emoticon_regex = (
            r"(?:\*?[03<>]|\(\)|3>)?(?:[¦|:;=38BEXxz@%+]|\(:\))'?[-'o^~]?"
            + r"[\)\(\|\]\[}{>3DdQPpOoXxSs@€*$#/\\°~]|[\)\(\|\]\[}{>3DdQPpOoXxSs@€*$#/\\°~]"
            + r"[-'o^~]?'?(?:[¦|:;=38BEXxz@%+]|\(:\))(?:\*?[03<>]|\(\)|3>)?|<(?:/|\\)?3"
            + r"|&\[ \]|\[]==\[]|\(___\(--#|>°\)+><|69|\(\.\\°\)|\([_ ][Y¤)()][_ ]\)"
            + r"|\( [o\.] (?:\)\(|Y) [o\.] \)|(?:\(_\)_\)|[c83])=+[D3]|{\('\)}|_@_/|i@_|@}[->',]+"
        )
        self.emoticon_matcher = re.compile(
            emoticon_regex,
            flags
        )

    async def start(self):
        await self.stop()
        self._task = asyncio.create_task(self._transcript_reply_task())

    async def stop(self):
        if not self._task:
            return

        self._abort = True
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=self.TASK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            fancy_logger.get().warning("audio_responder: task did not quit in time")
        except asyncio.CancelledError:
            fancy_logger.get().info("audio_responder: task stopped")
        self._task = None
        self._abort = False

    # @fancy_logger.log_async_task
    async def _transcript_reply_task(self):
        fancy_logger.get().info("audio_responder: started")
        def _clear_response_task():
            if self._response_task:
                self._response_task = None
        try:
            while not self._abort:
                await self._transcript.wakeword_event.wait()
                self._transcript.wakeword_event.clear()
                response_task = asyncio.create_task(self._respond())
                response_task.add_done_callback(
                    lambda _: _clear_response_task()
                )
                if not self._is_responding():
                    self._response_task = response_task
                else:
                    await response_task
        except asyncio.CancelledError:
            self._response_queue.clear()
            await self._cancel_response_task()

        fancy_logger.get().info("audio_responder: exiting")

    async def _respond(self):
        transcript_history = self._transcript_history_iterator()
        voice_messages = self._transcript.message_buffer.get()
        voice_messages.reverse()
        user_id = voice_messages[-1].user_id
        user_name = ""
        author = discord_utils.author_from_user_id(
            user_id,
            self._channel.guild,
        )
        user_name = author.author_name if author else "<unknown user>"
        fancy_logger.get().debug(
            "Responding to message from %s in %s",
            user_name, self._channel.name
        )
        prompt, author_names = await self._prompt_generator.generate(
            bot_user_id=self.bot_user_id,
            message_history=transcript_history,
            user_name=user_name,
            guild_name=self._channel.guild.name,
            channel_name=self._channel.name
        )
        stop_sequences: typing.List[str] = []

        if self.prevent_impersonation:
            # Utility functions to avoid code-duplication
            def _get_user_prompt_prefix(user_name: str) -> str:
                return self._template_store.format(
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

        response = await self._ooba_client.request_as_string(
            prompt, stop_sequences
        )
        # filter immersion-breaking content
        if self.use_immersion_breaking_filter:
            response, _should_abort = self.immersion_breaking_filter.filter(response)

        # wait for silence before responding
        await self._transcript.silence_event.wait()

        if self._first_response and bool(self._response_queue):
            self._first_response = False
        # queue response
        fancy_logger.get().debug("Queueing response: %s", response)
        self._response_queue.appendleft(response)
        # abort if already processing response queue
        if not self._first_response and self._is_responding():
            return
        # process queue, if not already responding
        while self._response_queue:
            response = self._response_queue.pop()
            # shove response into history
            self._transcript.on_bot_response(response)
            # post and/or speak response
            if self.post_voice_responses:
                text_response = response
                kwargs = {}
                # mention user if there are multiple participants and the last user
                # responded to is different, to make it clear who is being spoken to
                if (
                    #self._transcript.num_participants > 1
                    #and user_id != self._transcript.last_response_user_id
                    user_id != self._transcript.last_response_user_id
                ):
                    text_response = f"<@{user_id}> {text_response}"
                    # don't send the user a notification
                    kwargs["allowed_mentions"] = discord.AllowedMentions(users=False)
                # post raw response, if configured to do so
                await self._channel.send(
                    text_response,
                    suppress_embeds=True,
                    silent=True,
                    **kwargs
                )
            # speak response aloud
            if self.speak_voice_responses:
                dialogue = response
                # extract sanitized dialogue from response
                dialogue = self.emoticon_matcher.sub("", dialogue) # remove "emoticons"
                dialogue = emoji.replace_emoji(dialogue, "") # remove actual emoji
                # collapse consecutive spaces and newlines into a single space
                dialogue = re.sub(r"\b\s+\b", " ", dialogue, re.MULTILINE)
                # get all valid dialogue and drop any narration
                dialogue = self.dialogue_matcher.findall(dialogue)
                dialogue = " ".join([block[0].strip() for block in dialogue]).strip()
                # finally, speak the dialogue
                self._discrivener.speak(dialogue)
            fancy_logger.get().debug("Response to %s done!", user_name)

    async def _cancel_response_task(self) -> None:
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            await self._response_task

    def _is_responding(self) -> bool:
        if self._response_task:
            return not self._response_task.done()
        return False

    def _transcript_history_iterator(
        self,
    ) -> typing.AsyncIterator[types.GenericMessage]:
        voice_messages = self._transcript.message_buffer.get()
        voice_messages.sort(key=lambda message: message.start_time, reverse=True)

        # create an async generator which iterates over the lines
        # in the transcript
        async def _gen():
            for message in voice_messages:
                author = discord_utils.author_from_user_id(
                    message.user_id,
                    self._channel.guild,
                )
                if author:
                    author_name = author.author_name
                    author_is_bot = author.author_is_bot
                else:
                    author_name = f"user #{message.user_id}"
                    author_is_bot = message.is_bot
                yield types.GenericMessage(
                    author_id=message.user_id,
                    author_name=author_name,
                    channel_id=self._channel.id,
                    channel_name=self._channel.name,
                    message_id=0,
                    reference_message_id=0,
                    body_text=message.text,
                    author_is_bot=author_is_bot,
                    send_timestamp=message.start_time.timestamp(),
                )

        return _gen()

    def _get_latest_message(self) -> types.VoiceMessage:
        voice_messages = self._transcript.message_buffer.get()
        voice_messages.reverse()
        return voice_messages[-1]
