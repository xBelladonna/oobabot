# -*- coding: utf-8 -*-
"""
Detects when the bot is repeating previous messages, and attempts
to fix this by hiding the messages that it's repeating from its view
of the chat history.

Is also used to implement the /lobotomize command, which is the same
thing except it's triggered by a command instead of automatically,
and is tracked separately to repetition throttle messages.
"""
import typing
from thefuzz import fuzz

from oobabot import fancy_logger
from oobabot import types


class RepetitionTracker:
    """
    Tracks the last message the bot posted in each channel, and the number of times
    in a row it has been repeated.
    
    Also keeps track of any message we've explicitly
    logged as a history marker, i.e. the message which marks the beginning of the
    AI's chat history.
    """

    def __init__(
        self,
        discord_settings: typing.Dict
    ) -> None:
        self.repetition_threshold = discord_settings["repetition_threshold"]
        self.similarity_threshold = discord_settings["repetition_similarity_threshold"]

        # stores a map of channel_id ->
        #   (last_message, throttle_message_id, repetition_count)
        self.repetition_count: typing.Dict[int, typing.Tuple[str, int, int]] = {}
        # stores a map of channel_id -> history_marker_id
        self.history_markers: typing.Dict[int, int] = {}

    def get_throttle_message_id(self, channel_id: int) -> int:
        """
        Returns the message ID of the last message that should be throttled, or 0
        if no throttling is needed.
        """
        _, throttle_message_id, repetition_count = self.repetition_count.get(
            channel_id, (None, 0, 0)
        )
        return throttle_message_id if self.should_throttle(repetition_count) else 0

    def clear_throttle_message_id(self, channel_id: int) -> bool:
        """
        Clears the provided channel's throttle message entry. Returns True if
        there was a message and it was cleared, or False if there was no
        throttle message and nothing was done.
        """
        try:
            self.repetition_count.pop(channel_id)
            return True
        except KeyError:
            return False

    def get_history_marker_id(self, channel_id: int) -> int:
        """
        Returns the message ID of the message that marks the beginning of the
        AI's chat history, or 0 if there is no history marker logged.
        """
        return self.history_markers.get(channel_id, 0)

    def clear_history_marker_id(self, channel_id: int) -> bool:
        """
        Clears the provided channel's history marker message. Returns True if
        there was a marker and it was cleared, or False if there was no
        marker and nothing was done.
        """
        try:
            self.history_markers.pop(channel_id)
            return True
        except KeyError:
            return False

    def hide_messages_before(self, channel_id: int, message_id: int) -> None:
        """
        Hides all messages before the given message ID in the given channel.
        """
        fancy_logger.get().info(
            "Hiding messages before message ID %d in channel %d", message_id, channel_id
        )
        self.history_markers[channel_id] = message_id

    def log_message(
        self, channel_id: int, response_message: types.GenericMessage
    ) -> None:
        """
        Logs a message sent by the bot, to be used for repetition tracking.
        """
        # make string into canonical form
        message_text = self.make_canonical(response_message.body_text)
        last_message_text, throttle_message_id, repetition_count = self.repetition_count.get(
            channel_id, ("", 0, 0)
        )

        repetition_found = False
        if last_message_text == message_text:
            repetition_found = True
        elif self.similarity_threshold:
            similarity_score = fuzz.token_set_ratio(last_message_text, message_text) / 100
            if similarity_score >= self.similarity_threshold:
                repetition_found = similarity_score

        if repetition_found:
            repetition_count += 1
        else:
            repetition_count = 0

        if repetition_count > 0:
            fancy_logger.get().debug(
                "Repetition count for channel %d is %d", channel_id, repetition_count
            )

        if self.should_throttle(repetition_count):
            if repetition_found is True:
                repetition_details = "exact match"
            else:
                repetition_details = f"similarity score: {repetition_found:.2f}"
            fancy_logger.get().warning(
                "Repetition found (%s), will throttle history for channel %d "
                + "in next request",
                repetition_details,
                channel_id
            )
            throttle_message_id = response_message.message_id

        self.repetition_count[channel_id] = (
            message_text,
            throttle_message_id,
            repetition_count
        )

    def should_throttle(self, repetition_count: int) -> bool:
        """
        Returns whether the bot should throttle history for a given repetition count.
        """
        return False if not repetition_count or not self.repetition_threshold else (
            repetition_count >= self.repetition_threshold
        )

    def make_canonical(self, content: str) -> str:
        return content.strip().lower()
