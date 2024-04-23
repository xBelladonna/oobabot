# -*- coding: utf-8 -*-
"""
Decides whether the bot responds to a message.
"""

import random
import typing

from oobabot import fancy_logger
from oobabot import persona
from oobabot import types


class LastReplyTimes(dict):
    """
    A dictionary that keeps track of the last time we were mentioned
    in a channel.

    This uses the timestamp on the message, not the local system's
    RTC.  The advantage of this is that if messages are delayed,
    we'll only respond to ones that were actually sent within the
    appropriate time window.  It also makes it easier to test.
    """

    def __init__(self, cache_timeout: float, unsolicited_channel_cap: int):
        self.cache_timeout = cache_timeout
        self.unsolicited_channel_cap = unsolicited_channel_cap

    def purge_outdated(self, latest_timestamp: float) -> None:
        oldest_time_to_keep = latest_timestamp - self.cache_timeout

        if self.unsolicited_channel_cap > 0:
            # find the n-th largest timestamp
            if self.unsolicited_channel_cap < len(self):
                nth_largest_timestamp = sorted(self.values())[
                    -self.unsolicited_channel_cap
                ]
                oldest_time_to_keep = max(oldest_time_to_keep, nth_largest_timestamp)
        purged = {
            channel_id: response_time
            for channel_id, response_time in self.items()
            if response_time >= oldest_time_to_keep
        }
        self.clear()
        self.update(purged)

    def log_mention(self, channel_id: int, send_timestamp: float) -> None:
        self[channel_id] = send_timestamp

    def time_since_last_mention(self, message: types.ChannelMessage) -> float:
        self.purge_outdated(message.send_timestamp)
        return message.send_timestamp - self.get(message.channel_id, 0)


class DecideToRespond:
    """
    Decide whether to respond to a message.
    """

    def __init__(
        self,
        discord_settings: dict,
        persona: persona.Persona,
        interrobang_bonus: float,
        time_vs_response_chance: typing.List[typing.Tuple[float, float]],
    ):
        self.disable_unsolicited_replies = discord_settings[
            "disable_unsolicited_replies"
        ]
        self.ignore_dms = discord_settings["ignore_dms"]
        self.ignore_bots = discord_settings["ignore_bots"]
        self.ignore_prefixes = discord_settings["ignore_prefixes"]
        self.self_response_allowed = False
        self.interrobang_bonus = interrobang_bonus
        self.persona = persona
        self.time_vs_response_chance = time_vs_response_chance

        last_reply_cache_timeout = max(time for time, _ in time_vs_response_chance)
        unsolicited_channel_cap = discord_settings["unsolicited_channel_cap"]
        self.last_reply_times = LastReplyTimes(
            last_reply_cache_timeout,
            unsolicited_channel_cap,
        )

    def is_directly_mentioned(
        self, our_user_id: int, message: types.GenericMessage
    ) -> bool:
        """
        Returns True if the message is a direct message to us, or if it
        mentions us by @name or wakeword.
        """

        # reply to all private messages
        if isinstance(message, types.DirectMessage):
            if self.ignore_dms:
                return False
            return True

        # reply to all messages in which we're @-mentioned
        if isinstance(message, types.ChannelMessage):
            if message.is_mentioned(our_user_id):
                return True

        # reply to all messages that include a wakeword
        if self.persona.contains_wakeword(message.body_text):
            return True

        return False

    def calc_interpolated_response_chance(
        self,
        time_since_last_mention: float,
        time_vs_response_chance: typing.List[typing.Tuple[float, float]],
    ):
        """
        Calculates a linearly interpolated response chance between
        the current and next calibration entries, based on the exact
        duration since the last mention.
        """
        response_chance = 0.0
        if time_vs_response_chance:
            duration = 0
            chance = time_vs_response_chance[0][1]
            for next_duration, next_chance in time_vs_response_chance:
                if duration <= time_since_last_mention <= next_duration:
                    scaling_factor = (time_since_last_mention - duration) / (next_duration - duration)
                    response_chance = chance + (next_chance - chance) * scaling_factor
                    break
                duration, chance = next_duration, next_chance
        else:
            response_chance = 1.0
        return response_chance

    def provide_unsolicited_reply_in_channel(
        self, our_user_id: int, message: types.ChannelMessage
    ) -> bool:
        """
        Returns True if we should respond to the message, even
        though we weren't directly mentioned.
        """

        # if we're not at-mentioned but others are, don't reply
        if message.mentions and not message.is_mentioned(our_user_id):
            return False

        # if message is empty, don't reply.  This can happen if someone
        # posts an image or an attachment without a comment.
        if message.is_empty():
            return False

        # if we've posted recently in this channel, there are a few
        # other reasons we may respond.  But if we haven't, just
        # ignore the message.

        # if the admin has disabled unsolicited replies, don't reply
        if self.disable_unsolicited_replies:
            return False

        # if we haven't posted to this channel recently, don't reply
        time_since_last_mention = self.last_reply_times.time_since_last_mention(message)
        response_chance = self.calc_interpolated_response_chance(
            time_since_last_mention,
            self.time_vs_response_chance,
        )
        if not response_chance:
            return False

        # if the new message ends with a question mark, we'll respond
        if message.body_text.endswith("?"):
            response_chance += self.interrobang_bonus

        # if the new message ends with an exclamation point, we'll respond
        if message.body_text.endswith("!"):
            response_chance += self.interrobang_bonus

        time_since_last_mention = self.last_reply_times.time_since_last_mention(message)
        fancy_logger.get().debug(
            "Considering unsolicited response in channel %s after %2.0f seconds.  "
            + "chance: %2.0f%%.",
            message.channel_name,
            time_since_last_mention,
            response_chance * 100.0,
        )

        if random.random() <= response_chance:
            return True

        return False

    def should_reply_to_message(
        self, our_user_id: int, message: types.GenericMessage
    ) -> typing.Tuple[bool, bool]:
        """
        Returns a tuple of (should_reply, is_direct_mention).

        Direct mentions are always replied to, but also, the
        caller should log the mention later by calling log_mention().

        The only reason this method doesn't to so itself is that
        in the case of us generating a thread to reply on, the
        channel ID we want to track will be that of the thread
        we create, not the channel the message was posted in.
        """

        # ignore messages from other bots, out of fear of infinite loops,
        # as well as world domination
        if message.author_is_bot and self.ignore_bots:
            return (False, False)

        # we do not want the bot to reply to itself.  This is redundant
        # with the previous check, except it won't be if someone decides
        # to run this under their own user token, rather than a proper
        # bot token, or if they allow responding to other bots.
        if message.author_id == our_user_id:
            if self.self_response_allowed:
                # Disallow self-responses, so this setting can't be accidentally latched on
                self.self_response_allowed = False
                return (True, False)
            return (False, False)
        
        # ignore any messages explicitly flagged to be ignored
        for ignore_prefix in self.ignore_prefixes:
            if message.body_text.startswith(ignore_prefix):
                return (False, False)

        if self.is_directly_mentioned(our_user_id, message):
            return (True, True)

        if isinstance(message, types.ChannelMessage):
            if self.provide_unsolicited_reply_in_channel(our_user_id, message):
                return (True, False)

        # ignore anything else
        return (False, False)

    def log_mention(self, channel_id: int, send_timestamp: float) -> None:
        self.last_reply_times.log_mention(channel_id, send_timestamp)

    def get_unsolicited_channel_cap(self) -> int:
        return self.last_reply_times.unsolicited_channel_cap
