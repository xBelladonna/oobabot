# -*- coding: utf-8 -*-
"""
Converts Discord library objects into generic objects that can be used by the AI

This is done to make it easier to swap out the Discord library for something else
in the future, and to make it easier to test the AI without having to mock the
Discord library.
"""


import base64
import functools
import os
import pathlib
import re
import typing

import discord

from oobabot import fancy_logger
from oobabot import types

FORBIDDEN_CHARACTERS = r"[\n\r\t]"
FORBIDDEN_CHARACTERS_PATTERN = re.compile(FORBIDDEN_CHARACTERS)


def get_channel_name(
    channel: discord.abc.Messageable,
    with_type: bool = True
) -> str:
    """
    Fetches the provided channel's name, optionally prefixed
    with the channel type.
    """
    name_str = ""
    if isinstance(channel, discord.TextChannel):
        if with_type:
            name_str += "channel "
        name_str += "#" + channel.name
    elif isinstance(channel, discord.Thread):
        if with_type:
            name_str += "thread "
        name_str += "#" + channel.name
    elif isinstance(channel, discord.VoiceChannel):
        if with_type:
            name_str += "voice channel "
        name_str += "#" + channel.name
    elif isinstance(channel, discord.DMChannel):
        if channel.recipient:
            if with_type:
                name_str += "DM: "
            name_str += channel.recipient.display_name
        else:
            name_str = "Direct Message"
    elif isinstance(channel, discord.GroupChannel):
        if with_type:
            name_str += "Group DM: "
        name_str += channel.name or ", ".join(
            [user.display_name for user in channel.recipients]
        )
    else:
        return "-Unknown-"
    return sanitize_string(name_str)


def sanitize_string(raw_string: str) -> str:
    """
    Filter out any characters that would confuse the AI
    """
    # Ignore sanitizing of messages because it's really a non issue.
    return raw_string


def discord_message_to_generic_message(
    raw_message: discord.Message,
) -> typing.Union[
    types.GenericMessage,
    types.DirectMessage,
    types.GroupMessage,
    types.ChannelMessage
]:
    """
    Convert a discord message to a GenericMessage or subclass thereof
    """
    channel = raw_message.channel
    generic_args = {
        "author_id": raw_message.author.id,
        "author_name": sanitize_string(raw_message.author.display_name),
        "channel_id": raw_message.channel.id,
        "channel_name": get_channel_name(channel),
        "message_id": raw_message.id,
        "body_text": sanitize_string(raw_message.content),
        "author_is_bot": raw_message.author.bot,
        "send_timestamp": raw_message.created_at.timestamp(),
        "reference_message_id": raw_message.reference.message_id
        if raw_message.reference and raw_message.reference.message_id
        else 0
    }
    if isinstance(channel, discord.DMChannel):
        return types.DirectMessage(**generic_args)
    if isinstance(channel, discord.GroupChannel):
        return types.GroupMessage(
            mentions=[mention.id for mention in raw_message.mentions],
            **generic_args
        )
    if isinstance(
        channel,
        (
            discord.TextChannel,
            discord.Thread,
            discord.VoiceChannel
        )
    ):
        return types.ChannelMessage(
            guild_id=channel.guild.id,
            guild_name=channel.guild.name,
            mentions=[mention.id for mention in raw_message.mentions],
            **generic_args
        )
    fancy_logger.get().warning(
        f"Unknown channel type {type(raw_message.channel)}, "
        + f"unsolicited replies disabled: {raw_message.channel}"
    )
    return types.GenericMessage(**generic_args)


def replace_user_mention_ids_with_names(
    generic_message: types.GenericMessage,
    fn_user_id_to_name: typing.Callable[[re.Match[str]], str]
):
    """
    Replace user ID mentions with the user's chosen display
    name in the given guild (aka server)
    """
    # it looks like normal IDs are 18 digits. But give it some
    # wiggle room in case things change in the future.
    # e.g.: <@009999999999999999>
    at_mention_pattern = r"<@(\d{16,20})>"
    while True:
        match = re.search(at_mention_pattern, generic_message.body_text)
        if not match:
            break
        generic_message.body_text = (
            generic_message.body_text[:match.start()]
            + fn_user_id_to_name(match)
            + generic_message.body_text[match.end():]
        )


async def replace_channel_mention_ids_with_names(
    client: discord.Client,
    generic_message: types.GenericMessage
):
    """
    Replace user ID mentions with the user's chosen display
    name in the given guild (aka server)
    """
    # it looks like IDs are 19 digits long
    hash_mention_pattern = r"<#(\d{17,21})>"
    while True:
        match = re.search(hash_mention_pattern, generic_message.body_text)
        if not match:
            break
        channel_id = int(match.group(1))
        channel = (
            client.get_channel(channel_id)
            or await client.fetch_channel(channel_id)
        )
        if channel:
            channel_name = get_channel_name(
                channel, # type: ignore
                with_type=False
            )
            if isinstance(channel, discord.Thread):
                channel_name = "🧵" + channel_name
            if " " in channel_name:
                channel_name = f'"{channel_name}"'
        else:
            channel_name = "unknown-channel"

        channel_name = "#" + channel_name
        generic_message.body_text = (
            generic_message.body_text[:match.start()]
            + channel_name
            + generic_message.body_text[match.end():]
        )


def replace_emoji_ids_with_names(
    client: discord.Client,
    generic_message: types.GenericMessage,
):
    """
    Replace user ID mentions with the user's chosen display
    name in the given guild (aka server)
    """
    # it looks like IDs are 19 digits long
    emoji_pattern = r"<:(\S+):(\d{17,21})>"
    while True:
        match = re.search(emoji_pattern, generic_message.body_text)
        if not match:
            break
        emoji_id = int(match.group(2))
        emoji = client.get_emoji(emoji_id)
        emoji_name = emoji.name if emoji else match.group(1)
        generic_message.body_text = (
            generic_message.body_text[:match.start()]
            + f":{emoji_name}:"
            + generic_message.body_text[match.end():]
        )


def dm_user_id_to_name(
    bot_user_id: int,
    bot_name: str,
    user_name: str
) -> typing.Callable[[re.Match[str]], str]:
    """
    Replace user ID mentions with the bot's name. Used when
    we are in a DM with the bot.
    """
    if " " in bot_name:
        bot_name = f'"{bot_name}"'
    if " " in user_name:
        user_name = f'"{user_name}"'

    def _replace_user_id_mention(match: typing.Match[str]) -> str:
        user_id = int(match.group(1))
        if user_id == bot_user_id:
            return f"@{bot_name}"
        return f"@{user_name}"

    return _replace_user_id_mention


def group_user_id_to_name(
   group: discord.GroupChannel
) -> typing.Callable[[re.Match[str]], str]:
    def _replace_user_id_mention(match: typing.Match[str]) -> str:
        user_id = int(match.group(1))
        member = None
        for user in group.recipients:
            if user.id == user_id:
                member = user
                break
        if not member:
            return '@"Unknown member"'
        display_name = member.display_name
        if " " in display_name:
            display_name = f'"{display_name}"'
        return f"@{display_name}"

    return _replace_user_id_mention


def guild_user_id_to_name(
    guild: discord.Guild
) -> typing.Callable[[re.Match[str]], str]:
    def _replace_user_id_mention(match: typing.Match[str]) -> str:
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        if not member:
            return '@"Unknown member"'
        display_name = member.display_name
        if " " in display_name:
            display_name = f'"{display_name}"'
        return f"@{display_name}"

    return _replace_user_id_mention


def get_intents(presence: bool = True) -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.presences = presence
    return intents


async def test_discord_token(discord_token: str) -> bool:
    class _SimplestBot(discord.Client):
        async def on_ready(self):
            self.has_connected = True
            await self.close()

        def __init__(self):
            super().__init__(intents=get_intents())
            self.has_connected = False

    simplest_bot = _SimplestBot()
    try:
        await simplest_bot.start(discord_token, reconnect=False)
    except discord.errors.ConnectionClosed as err:
        # in theory, discord.errors.PrivilegedIntentsRequired
        # should get fired in this case, but it doesn't
        if err.code != 4014:
            raise
        fancy_logger.get().warning(
            "The bot token you provided does not have the required "
            + "gateway intents. Did you remember to enable both "
            + "'SERVER MEMBERS INTENT' and 'MESSAGE CONTENT INTENT' "
            + "in the bot's settings on Discord?"
        )
        return False

    except discord.LoginFailure:
        return False
    finally:
        await simplest_bot.close()
    return simplest_bot.has_connected


def get_user_id_from_token(discord_token: str) -> int:
    """
    Extract the bot's user ID from the discord token.
    """

    # turns out, the discord_token includes our client ID, so we can just
    # extract it from there.
    #
    # the discord token has this format:
    # AAAAAAAAAAAAAAAAAAAAAAAAAA.BBBBBB.CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
    #
    # where each section, A, B, and C, is independently a base64-encoded string.
    #
    # Section A encodes the bot's client ID, which is the decimal encoding of
    # a 64-bit number in the range [21154535154122752, 18446744073709551615]
    # (17 to 20 digits long).
    #
    # The other sections aren't important here.
    token_parts = discord_token.split(".")
    token_part_a = token_parts[0]

    # the base64 decoder requires the string to be a multiple of 4 characters
    # long, so we need to add padding
    if len(token_part_a) % 4 != 0:
        token_part_a += "=" * (4 - len(token_part_a) % 4)

    return int(base64.b64decode(token_part_a).decode("utf-8"))


def generate_invite_url(bot_user_id: int) -> str:
    # we want to generate a URL like this:
    # https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions={permissions}}&scope=bot
    #
    # where {client_id} is the bot's client ID, and {permissions} is the
    # permissions bit array with our desired permissions set.

    # for the permissions bit array, we can generate it with the library
    permissions = discord.Permissions(
        change_nickname=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
        manage_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        read_messages=True,
        add_reactions=True,
        connect=True,
        speak=True,
        use_voice_activation=True
    ).value

    return (
        "https://discord.com/api/oauth2/authorize?client_id="
        + f"{bot_user_id}&permissions={permissions}&scope=bot"
    )


def setup_logging(**kwargs: typing.Any):
    discord.utils.setup_logging(**kwargs)


async def fail_interaction(
    interaction: discord.Interaction, reason: typing.Optional[str] = None
):
    command = "unknown command"
    if interaction.command:
        command = interaction.command.name

    if reason is None:
        reason = f"{command} failed"

    fancy_logger.get().warning(
        "interaction failed: command='%s', user='%s', channel='%s', reason='%s'",
        command,
        interaction.user,
        interaction.channel,
        reason,
    )

    await interaction.response.send_message(reason, ephemeral=True, silent=True)


def _file_exists_and_is_file(filepath: typing.Optional[str]) -> typing.Optional[str]:
    if not filepath:
        return None

    path = pathlib.Path(filepath).expanduser()
    if not path.is_file():
        return None

    if not os.access(path, os.R_OK):
        return None

    return str(path.resolve())


def validate_discrivener_locations(
    discrivener_location: str,
    discrivener_model_location: str
) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
    """
    Verify that the file discrivener_location exists
    and is a file.

    If that passes, also checks that discrivener_model_location
    exists and is a file.

    Returns the expanded paths to each files if it exists,
    or None if it doesn't.

    Returns the tuple of these checks for the two passed files.
    """
    actual_discrivener_location = _file_exists_and_is_file(discrivener_location)
    # check that the discrivener binary is executable as well
    if actual_discrivener_location:
        if not os.access(actual_discrivener_location, os.X_OK):
            fancy_logger.get().warning(
                "discrivener binary is not executable: %s",
                actual_discrivener_location,
            )
            actual_discrivener_location = None

    actual_model_location = _file_exists_and_is_file(discrivener_model_location)
    return actual_discrivener_location, actual_model_location


# the following class was modified from O'Reilly's Python Cookbook,
# chapter 5, section 19. Its use is allowed under this license:
# Copyright (c) 2001, Sébastien Keim
# All rights reserved.
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials provided
#      with the distribution.
#    * Neither the name of the <ORGANIZATION> nor the names of its
#      contributors may be used to endorse or promote products derived
#      from this software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS
# OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

T = typing.TypeVar("T")
S = typing.TypeVar("S")


class RingBuffer(typing.Generic[T]):
    """
    A generic ring buffer.
    """

    def __init__(self, size_max: int):
        self.cur = 0
        self.max = size_max
        self.data: typing.List[T] = []

    class _FullRingBuffer(typing.Generic[S]):
        """
        Class implementing the RingBuffer when it's full.
        With python class magic, this class is swapped in when the
        buffer becomes full.
        """

        cur: int
        max: int
        data: typing.List[S]

        def append(self, val: S) -> None:
            """
            Append an element overwriting the oldest one.
            """
            self.data[self.cur] = val
            self.cur = (self.cur + 1) % self.max

        def get(self) -> typing.List[S]:
            """
            Return a list of elements from the oldest to the newest.
            """
            return self.data[self.cur :] + self.data[: self.cur]

        def size(self) -> int:
            """
            Return the size of the buffer.
            """
            return self.max

    def append(self, val: T) -> None:
        """
        Append an element at the end of the buffer.
        """
        self.data.append(val)
        if len(self.data) == self.max:
            self.cur = 0
            # Permanently change self's class from non-full to full
            self.__class__ = self._FullRingBuffer

    def get(self) -> typing.List[T]:
        """
        Return a list of elements from the oldest to the newest.
        """
        return self.data

    def size(self) -> int:
        """
        Return the number of elements currently in the buffer.
        """
        return len(self.data)


# end of O'Reilly code


@functools.lru_cache
def author_from_user_id(
    user_id: int,
    guild: discord.Guild,
) -> typing.Optional["types.FancyAuthor"]:
    member = guild.get_member(user_id)
    if not member:
        return None
    if member.avatar:
        avatar_url = member.avatar.url
    else:
        avatar_url = None
    if member.accent_color:
        accent_color = member.accent_color.to_rgb()
    else:
        accent_color = (0, 0, 0)
    return types.FancyAuthor(
        user_id=user_id,
        author_is_bot=member.bot,
        author_name=member.display_name,
        author_accent_color=accent_color,
        author_avatar_url=avatar_url,
    )
