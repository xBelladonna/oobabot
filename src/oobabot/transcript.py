# -*- coding: utf-8 -*-
"""
Stores a transcript of a voice channel.
"""
import asyncio
import datetime
import random
import typing

from oobabot import discord_utils
from oobabot import discrivener_message
from oobabot import decide_to_respond
from oobabot import fancy_logger
from oobabot import types


class Transcript:
    """
    Stores a transcript of a voice channel.
    """

    NUM_LINES = 300

    def __init__(
        self,
        bot_user_id: int,
        wakewords: typing.List[str],
        decide_to_respond: decide_to_respond.DecideToRespond,
    ):
        self._bot_user_id = bot_user_id
        self._wakewords: typing.Set[str] = set(word.lower() for word in wakewords)

        self.message_buffer = discord_utils.RingBuffer[types.VoiceMessage](
            self.NUM_LINES
        )
        self.decide_to_respond = decide_to_respond
        self.silence_event = asyncio.Event()
        self.wakeword_event = asyncio.Event()
        self.last_mention = datetime.datetime.min
        self.last_response_user_id = 0
        self.num_participants = 0

    def on_bot_response(self, text: str):
        """
        Adds a bot response to the transcript.
        """
        self.message_buffer.append(BotVoiceMessage(self._bot_user_id, text))

    def on_transcription(
        self,
        message: discrivener_message.UserVoiceMessage,
    ) -> None:
        self.message_buffer.append(message)

        wakeword_found = False
        for wakeword in self._wakewords:
            if wakeword in message.text.lower():
                wakeword_found = True
                break

        now = datetime.datetime.now()
        if wakeword_found:
            fancy_logger.get().info("transcript: wakeword detected in: %s", message.text)
            self.last_mention = now
            self.wakeword_event.set()
        else:
            if self.last_mention is datetime.datetime.min:
                seconds_since_mention = 0
            else:
                seconds_since_mention = (now - self.last_mention).seconds
            users = set()
            for msg in self.message_buffer.get():
                if (
                    (self.decide_to_respond.ignore_bots and not msg.is_bot)
                    or msg.user_id != self._bot_user_id
                ):
                    users.add(msg.user_id)
                    self.last_response_user_id = msg.user_id
            self.num_participants = len(users)
            should_respond, response_chance = self.decide_to_respond.provide_voice_response(
                seconds_since_mention,
                self.num_participants
            )
            fancy_logger.get().debug(
                "transcript: %d%% chance of replying after %d seconds (users: %d)",
                round(response_chance * 100),
                seconds_since_mention,
                self.num_participants
            )
            if should_respond:
                self.wakeword_event.set()

    def on_channel_silent(
        self, activity: discrivener_message.ChannelSilentData
    ) -> None:
        if activity.silent:
            self.silence_event.set()
        else:
            self.silence_event.clear()


class BotVoiceMessage(types.VoiceMessage):
    """
    Represents a fake "transcribed" message generated by
    the bot. This isn't a real transcription, because we got
    it from the bot, not from Discrivener. But we're creating
    a similar object to store it in, so that we can use similar
    code to store and display it.
    """

    def __init__(
        self,
        bot_user_id: int,
        text: str,
    ):
        self._text = text
        super().__init__(
            user_id=bot_user_id,
            start_time=datetime.datetime.now(),
            duration=datetime.timedelta(seconds=1),
        )

    @property
    def text(self) -> str:
        return self._text

    @property
    def is_bot(self) -> bool:
        """
        Returns whether the user is a bot.
        """
        return True
