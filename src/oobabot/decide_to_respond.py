# -*- coding: utf-8 -*-
"""
Decides whether the bot responds to a message.
"""

import random
import typing

from oobabot import fancy_logger
from oobabot import persona
from oobabot import types


class LastMentionTimes(dict):
    """
    A dictionary that keeps track of the last time we were mentioned
    in a channel.

    This uses the timestamp on the message, not the local system's
    RTC. The advantage of this is that if messages are delayed,
    we'll only respond to ones that were actually sent within the
    appropriate time window. It also makes it easier to test.

    It also keeps track of "channel cooldowns", where the bot will
    not respond even if a recent mention is logged. This is used
    e.g. if a user issues /unpoke.
    """

    def __init__(self, cache_timeout: float, unsolicited_channel_cap: int):
        self.cache_timeout = cache_timeout
        self.unsolicited_channel_cap = unsolicited_channel_cap
        self.cooldowns: typing.Set[int] = set()

    def purge_outdated(self, latest_timestamp: float) -> bool:
        """
        Removes mentions older than the cache timeout. If the
        unsolicited channel cap is set, the oldest mentions
        will be evicted to keep within the limit, even if the
        mention timestamp was within the cache timeout.

        Returns True if any mentions were retained, False if
        there are no longer any mentions in the guild.
        """
        oldest_time_to_keep = latest_timestamp - self.cache_timeout

        if self.unsolicited_channel_cap:
            # find the n-th largest timestamp
            if len(self) > self.unsolicited_channel_cap:
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
        self.cooldowns.intersection_update(self)

        return bool(self)

    def log_mention(self, channel_id: int, send_timestamp: float) -> None:
        """
        Logs the provided timestamp to the provided channel as a mention.
        """
        self[channel_id] = send_timestamp
        self.cooldowns.discard(channel_id)

    def time_since_last_mention(
        self, message: types.ChannelMessage
    ) -> typing.Optional[float]:
        """
        Get the time in seconds since the last mention, starting from
        the timestamp of the provided message. If there is no recent
        mention in the channel, return None.
        """
        last_mention_timestamp = self.get(message.channel_id, None)
        if last_mention_timestamp is None:
            return None
        return message.send_timestamp - last_mention_timestamp


class DecideToRespond:
    """
    Decide whether to respond to a message.
    """

    def __init__(
        self,
        discord_settings: typing.Dict,
        persona: persona.Persona,
    ):
        self.disable_unsolicited_replies = discord_settings[
            "disable_unsolicited_replies"
        ]
        self.ignore_dms = discord_settings["ignore_dms"]
        self.ignore_bots = discord_settings["ignore_bots"]
        self.ignore_prefixes = discord_settings["ignore_prefixes"]
        self.interrobang_bonus = discord_settings["interrobang_bonus"]
        self.persona = persona

        self.time_vs_response_chance: typing.List[typing.Tuple[float, float]] = []
        self.voice_time_vs_response_chance: typing.List[typing.Tuple[float, float]] = []
        for setting in "time_vs_response_chance", "voice_time_vs_response_chance":
            table: typing.List[typing.Tuple[float, float]] = getattr(self, setting)
            for x in discord_settings.get(setting): # type: ignore
                x = str(x).replace("(", "").replace(")", "").replace(",", " ")
                x = tuple(map(float, x.split()))
                row: typing.Tuple[float, float] = (x[0], x[1])
                for value in row:
                    if value < 0:
                        raise ValueError(
                            "Durations and response chances in the "
                            + "time_vs_response_chance calibration tables can't be "
                            + "negative! Please fix your configuration."
                        )
                    table.append(row)
                    table.sort()

        # Keep a dict of channel mention timestamps, per guild
        self.last_mention_cache_timeout = max(
            time for time, _ in self.time_vs_response_chance
        )
        self.unsolicited_channel_cap = discord_settings["unsolicited_channel_cap"]
        self.last_mention_times: typing.Dict[int, LastMentionTimes] = {}

        # Keep a set of message IDs per channel, to track whether they are
        # guaranteed a response. Technically we only need to track message
        # ID but each set is nested under a channel ID to ensure we can
        # clear entries per-channel and prevent any potential memory leaks.
        self.guaranteed_responses: typing.Dict[int, typing.Set[int]] = {}

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

    def is_hidden_message(self, content: str) -> bool:
        hidden = False
        for prefix in self.ignore_prefixes:
            if content.startswith(prefix):
                hidden = True
                break

        return hidden

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
        # If our calibration table is empty, always respond
        if not time_vs_response_chance:
            return 1.0

        response_chance = 0.0
        duration = 0.0
        chance = time_vs_response_chance[0][1]
        for next_duration, next_chance in time_vs_response_chance:
            if duration <= time_since_last_mention <= next_duration:
                scaling_factor = (time_since_last_mention -
                                  duration) / (next_duration - duration)
                response_chance = chance + (next_chance - chance) * scaling_factor
                break
            duration, chance = next_duration, next_chance
        return response_chance

    def provide_unsolicited_response_in_channel(
        self, our_user_id: int, message: types.ChannelMessage
    ) -> bool:
        """
        Returns True if we should respond to the message, even
        though we weren't directly mentioned.
        """

        # if we're not at-mentioned but others are, don't respond
        if message.mentions and not message.is_mentioned(our_user_id):
            return False

        # if the admin has disabled unsolicited replies, don't respond
        if self.disable_unsolicited_replies:
            return False

        # get response chance based on when we were last mentioned in this channel
        time_since_last_mention = self.time_since_last_mention(message)
        if time_since_last_mention is None or self.is_cooling(
            message.guild_id, message.channel_id
        ):
            return False

        response_chance = self.calc_interpolated_response_chance(
            time_since_last_mention,
            self.time_vs_response_chance
        )

        if not response_chance:
            return False

        # if the message ends with a question mark,
        # increase response chance
        if message.body_text.endswith("?"):
            response_chance += self.interrobang_bonus
        # if the message ends with an exclamation point,
        # increase response chance
        if message.body_text.endswith("!"):
            response_chance += self.interrobang_bonus
        # clamp the upper-limit of the final chance at 100%
        response_chance = min(1.0, response_chance)

        fancy_logger.get().debug(
            "Considering unsolicited response in %s after %2.0f seconds. "
            + "chance: %2.0f%%.",
            message.channel_name,
            time_since_last_mention,
            response_chance * 100.0,
        )

        if random.random() <= response_chance:
            return True

        return False

    def provide_voice_response(
        self,
        time_since_last_mention: float,
        number_of_participants: int,
    ) -> typing.Tuple[bool, float]:
        """
        Returns a tuple of (should_reply, response_chance).
        Responses are guaranteed if in a 1:1 voice call, otherwise
        the response chance is calculated normally and divided by
        the number of call participants.
        """
        if number_of_participants == 1:
            return True, 1.0
        response_chance = self.calc_interpolated_response_chance(
            time_since_last_mention,
            self.voice_time_vs_response_chance,
        )
        if not response_chance:
            # Default to the response chance of the last duration in
            # the calibration table.
            response_chance = self.voice_time_vs_response_chance[-1][1]
        # Clamp the number of participants to a reasonable value
        # otherwise the response chance may become too low with a
        # very large number of participants.
        response_chance /= min(3, number_of_participants)
        if random.random() <= response_chance:
            return True, response_chance

        return False, response_chance

    def should_ignore_message(
        self, bot_user_id: int, message: types.GenericMessage
    ) -> bool:
        """
        Checks if the provided message should be ignored according
        to the following conditions:
        - If the message is from a bot and we're ignoring bot messages
        - If the message was from us
        - If the message is explicitly hidden

        Returns a boolean indicating if we should ignore the message.
        """
        # Ignore messages from other bots, out of fear of infinite loops,
        # as well as world domination.
        if message.author_is_bot and self.ignore_bots:
            return True

        # We do not want the bot to reply to itself. This is redundant
        # with the previous check, except it won't be if someone decides
        # to run this under their own user token, rather than a proper
        # bot token, or if they allow responding to other bots.
        if message.author_id == bot_user_id:
            return True

        # Ignore any hidden messages
        if self.is_hidden_message(message.body_text):
            return True

        # Respond to anything else
        return False

    def should_respond_to_message(
        self, bot_user_id: int, message: types.GenericMessage
    ) -> typing.Tuple[bool, bool]:
        """
        Returns a tuple of (should_respond, is_direct_mention).

        Direct mentions are always replied to, but also, the
        caller should log the mention later by calling log_mention().

        The only reason this method doesn't to so itself is that
        in the case of us generating a thread to reply on, the
        channel ID we want to track will be that of the thread
        we create, not the channel the message was posted in.
        """
        is_direct_mention = self.is_directly_mentioned(bot_user_id, message)

        guaranteed = self.guaranteed_responses.get(message.channel_id, None)
        if guaranteed and message.message_id in guaranteed:
            guaranteed.discard(message.message_id)
            # If the set is empty, remove the whole channel from the dict
            if not guaranteed:
                self.guaranteed_responses.pop(message.channel_id, None)
            return True, is_direct_mention

        if self.should_ignore_message(bot_user_id, message):
            return False, False

        if is_direct_mention:
            return True, True

        if (
            isinstance(message, types.ChannelMessage)
            and self.provide_unsolicited_response_in_channel(bot_user_id, message)
        ):
            return True, False

        # Ignore anything else
        return False, False

    def log_mention(
        self, guild_id: int, channel_id: int, send_timestamp: float
    ) -> None:
        """
        Log a mention for the provided channel in the corresponding guild.
        """
        # Ensure the guild has a corresponding tracker
        if guild_id not in self.last_mention_times:
            self.last_mention_times[guild_id] = LastMentionTimes(
                self.last_mention_cache_timeout,
                self.unsolicited_channel_cap
            )
        # Log the mention
        self.last_mention_times[guild_id].log_mention(channel_id, send_timestamp)

    def log_cooldown(self, guild_id: int, channel_id: int) -> None:
        last_mention_times = self.last_mention_times.get(guild_id, None)
        # Only log cooldown if we're paying attention to the channel
        if last_mention_times and channel_id in last_mention_times:
            last_mention_times.cooldowns.add(channel_id)

    def is_cooling(self, guild_id: int, channel_id: int) -> bool:
        last_mention_times = self.last_mention_times.get(guild_id, None)
        if last_mention_times:
            return channel_id in last_mention_times.cooldowns
        return False

    def time_since_last_mention(
        self, message: types.ChannelMessage
    ) -> typing.Optional[float]:
        """
        Gets the time since last mentioned, in seconds, in the channel of the
        provided message, starting from its timestamp.
        """
        # Purge all channels and guilds without mentions within the cache timeout
        purged = {
            guild: last_mention_times
            for guild, last_mention_times in self.last_mention_times.items()
            if last_mention_times.purge_outdated(message.send_timestamp)
        }
        self.last_mention_times.clear()
        self.last_mention_times.update(purged)

        # Get guild last mention times
        last_mention_times = self.last_mention_times.get(message.guild_id, None)
        if not last_mention_times:
            # If we have not been mentioned in the guild within the cache timeout,
            # return None
            return None
        return last_mention_times.time_since_last_mention(message)

    def guarantee_response(self, channel_id: int, message_id: int) -> None:
        """
        Logs a flag that will guarantee a response to the provided message
        when it is processed, unless the queue is cancelled.
        """
        if channel_id not in self.guaranteed_responses:
            self.guaranteed_responses[channel_id] = set()
        self.guaranteed_responses[channel_id].add(message_id)

    def get_guarantees(self, channel_id: int) -> typing.Optional[
        typing.Set[int]
    ]:
        """
        Returns the specified channel's response guarantees, if any.
        """
        return self.guaranteed_responses.get(channel_id, None)

    def purge_guarantees(self, channel_id: int) -> None:
        """
        Purge all response guarantees in the provided channel.
        """
        self.guaranteed_responses.pop(channel_id, None)
