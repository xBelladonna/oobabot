# -*- coding: utf-8 -*-
"""
Generate a prompt for the AI to respond to, given the
message history and persona.
"""
from collections import deque
from datetime import datetime
import os
import typing
from zoneinfo import ZoneInfo

from oobabot import fancy_logger
from oobabot import ooba_client
from oobabot import persona
from oobabot import templates
from oobabot import types


class PromptGenerator:
    """
    Purpose: generate a prompt for the AI to use, given
    the message history and persona.
    """

    # The average number of characters in a token. This is used to
    # estimate the available context space. In practice, this is
    # highly dynamic and can't be estimated well, but we try anyway.
    EST_CHARACTERS_PER_TOKEN = 3

    # The estimated number of characters in a history message.
    # This is used to roughly calculate whether we'll have enough
    # space to supply the requested number of history messages at
    # startup, and warn the user if the configured history limit
    # might not fit. During operation, we look at the actual number
    # of characters to see what we can fit.
    #
    # Note that we're doing calculations in characters, not in tokens,
    # so even counting characters exactly is still an estimate.
    EST_CHARACTERS_PER_HISTORY_LINE = 30
    # When we're not splitting responses, each history message is
    # much larger, and it's easier to run out of token space,
    # so we use a different estimate
    EST_CHARACTERS_PER_HISTORY_LINE_NOT_SPLITTING_RESPONSES = 180

    def __init__(
        self,
        discord_settings: dict,
        oobabooga_settings: dict,
        persona: persona.Persona,
        template_store: templates.TemplateStore,
        ooba_client: ooba_client.OobaClient,
    ):
        self.persona = persona
        self.template_store = template_store
        self.ooba_client = ooba_client
        self.strip_prompt: bool = discord_settings["strip_prompt"]
        self.split_responses: bool = discord_settings["split_responses"]
        self.history_messages: int = discord_settings["history_messages"]
        self.secondary_prompt_depth: int = discord_settings["secondary_prompt_depth"]
        self.automatic_lookback: bool = discord_settings["automatic_lookback"]
        self.context_length: int = oobabooga_settings["request_params"]["truncation_length"]

        # this will be also used when sending message
        # to suppress sending the prompt text to the user
        self.bot_prompt_block = self.template_store.format(
            templates.Templates.BOT_PROMPT_HISTORY_BLOCK,
            {
                templates.TemplateToken.NAME: self.persona.ai_name,
                templates.TemplateToken.MESSAGE: ""
            }
        )

        # Get unformatted example dialogue template that
        # we will split and format manually later
        self.example_dialogue = self.template_store.get(
            templates.Templates.EXAMPLE_DIALOGUE
        ).strip()

        self.example_dialogue_template_tokens = {
            templates.TemplateToken.AI_NAME: self.persona.ai_name,
            templates.TemplateToken.SYSTEM_SEQUENCE_PREFIX: self.template_store.get(
                templates.Templates.SYSTEM_SEQUENCE_PREFIX
            ),
            templates.TemplateToken.SYSTEM_SEQUENCE_SUFFIX: self.template_store.get(
                templates.Templates.SYSTEM_SEQUENCE_SUFFIX
            ),
            templates.TemplateToken.USER_SEQUENCE_PREFIX: self.template_store.get(
                templates.Templates.USER_SEQUENCE_PREFIX
            ),
            templates.TemplateToken.USER_SEQUENCE_SUFFIX: self.template_store.get(
                templates.Templates.USER_SEQUENCE_SUFFIX
            ),
            templates.TemplateToken.BOT_SEQUENCE_PREFIX: self.template_store.get(
                templates.Templates.BOT_SEQUENCE_PREFIX
            ),
            templates.TemplateToken.BOT_SEQUENCE_SUFFIX: self.template_store.get(
                templates.Templates.BOT_SEQUENCE_SUFFIX
            )
        }

        if self.ooba_client.can_get_token_count():
            self.max_context_units = self.context_length - \
                oobabooga_settings["request_params"]["max_tokens"]
        else:
            self._init_history_available_chars()


    def _init_history_available_chars(self) -> None:
        """
        Calculate the number of tokens or characters we have available for
        history, and raise an exception if we don't have enough. Logs a
        warning if don't have room for the prompt or the number of
        requested history messages.
        """
        # the number of chars we have available for history is:
        # - number of chars in context (estimated) minus the number of chars in the prompt
        #     - without any history
        #     - but with the largest special request templated in

        max_length_display_name = "#" * 32 # we just need any string of the max length
        special_requests = [
            self.template_store.format(
                templates.Templates.PROMPT_IMAGE_COMING,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: max_length_display_name
                }
            ),
            self.template_store.format(
                templates.Templates.PROMPT_IMAGE_NOT_COMING,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: max_length_display_name
                }
            ),
            self.template_store.format(
                templates.Templates.PROMPT_REWRITE_REQUEST,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: max_length_display_name,
                    templates.TemplateToken.INSTRUCTION: "",
                }
            )
        ]
        prompt_without_history = self._render_prompt(
            "",
            max(special_requests, key=len),
            guild_name="",
            channel_name=""
        )

        est_chars_in_context = self.context_length * self.EST_CHARACTERS_PER_TOKEN
        # how many chars might we have available for history?
        available_chars_for_history = est_chars_in_context - len(prompt_without_history)
        self.max_context_units = available_chars_for_history

        # how many chars do we need for the requested number of history messages?
        if self.split_responses:
            chars_per_history_line = self.EST_CHARACTERS_PER_HISTORY_LINE
        else:
            chars_per_history_line = \
                self.EST_CHARACTERS_PER_HISTORY_LINE_NOT_SPLITTING_RESPONSES
        required_history_size_chars = self.history_messages * chars_per_history_line
        if available_chars_for_history <= 0:
            fancy_logger.get().warning(
                "AI context length is too small for the prompt alone by an estimated "
                + "%d characters. There is no space to add chat history and the bot "
                + "may not work at all. Please shorten the prompt and try again.",
                len(prompt_without_history) - est_chars_in_context
            )
        elif available_chars_for_history < required_history_size_chars:
            fancy_logger.get().warning(
                "AI context length is too small for prompt and history by an estimated "
                + "%d characters. You may lose chat history. You can save space by "
                + "shortening the persona fields or reducing the requested number of "
                + "history messages.",
                required_history_size_chars - available_chars_for_history
            )

    async def _get_unit_count(self, message_str: str) -> int:
        try:
            message_units = await self.ooba_client.get_token_count(message_str)
        except ValueError:
            message_units = len(message_str)

        return message_units

    async def _render_history(
        self,
        bot_user_id: int,
        message_history: typing.AsyncIterator[types.GenericMessage],
        system_message: str,
        guild_name: str,
        channel_name: str
    ) -> typing.Tuple[str, typing.List[str]]:
        """
        Renders the requested number of history messages to a text string,
        fully templated to be combined with the rest of the prompt. If
        we run out of room (either by estimation or token counting), we
        truncate the oldest messages so that the prompt fits within the
        model's context length.

        Also collects the message author display names and returns them
        in a list, ordered by most recently appearing to least recently
        appearing, to use as stop sequences for impersonation prevention
        purposes.
        """
        # history_messages is newest first, so figure out how many we can
        # take, then append them in reverse order
        history_messages: typing.Deque[str] = deque()
        author_names: typing.List[str] = []

        # Instruct templates are static
        user_sequence_prefix = self.template_store.get(
            templates.Templates.USER_SEQUENCE_PREFIX
        )
        user_sequence_suffix = self.template_store.get(
            templates.Templates.USER_SEQUENCE_SUFFIX
        )
        bot_sequence_prefix = self.template_store.get(
            templates.Templates.BOT_SEQUENCE_PREFIX
        )
        bot_sequence_suffix = self.template_store.get(
            templates.Templates.BOT_SEQUENCE_SUFFIX
        )

        section_separator = self.template_store.format(
            templates.Templates.SECTION_SEPARATOR,
            {
                templates.TemplateToken.AI_NAME: self.persona.ai_name
            }
        )
        prompt_without_history = self._render_prompt(
            "",
            system_message,
            guild_name,
            channel_name
        )
        try:
            prompt_units = await self.ooba_client.get_token_count(
                prompt_without_history
            )
            # BOS tokens are stripped from token counts. Add 1 to the count
            # if we are configured to (default is true).
            if self.ooba_client.request_params.get("add_bos_token", True):
                prompt_units += 1
        except ValueError:
            prompt_units = len(prompt_without_history)

        # First we process and append the chat transcript
        context_full = False
        message_count = 0
        async for message in message_history:
            if context_full:
                break
            if message.is_empty():
                continue

            # Insert secondary prompt as separate message. We append it before
            # the message because the chat history is in reverse-order.
            # Subtract 1 from the prompt depth since it isn't zero-indexed.
            # We don't add to the message count for this entry.
            if (
                self.secondary_prompt_depth
                and message_count == self.secondary_prompt_depth - 1
            ):
                message_str = self._render_secondary_prompt(
                    system_message, guild_name, channel_name
                )

                message_units = await self._get_unit_count(message_str)
                units_left = self.max_context_units - prompt_units
                if message_units >= units_left:
                    context_full = True
                    if message_units > units_left:
                        break

                prompt_units += message_units
                history_messages.appendleft(message_str)

            # Now add the message
            for attachment in message.attachments:
                if attachment.is_empty():
                    continue
                if attachment.content_type in ("image", "image_url"):
                    image_received = self.template_store.format(
                        templates.Templates.PROMPT_IMAGE_RECEIVED,
                        {
                            templates.TemplateToken.AI_NAME: self.persona.ai_name,
                            templates.TemplateToken.USER_NAME: message.author_name
                        }
                    )
                    description_str = image_received + attachment.description_text
                    message.body_text += "\n" + description_str

            if message.author_id == bot_user_id:
                message_str = bot_sequence_prefix
                message_str += self.template_store.format(
                    templates.Templates.BOT_PROMPT_HISTORY_BLOCK,
                    {
                        templates.TemplateToken.NAME: message.author_name,
                        templates.TemplateToken.MESSAGE: message.body_text
                    }
                )
                message_str += bot_sequence_suffix
            else:
                message_str = user_sequence_prefix
                message_str += self.template_store.format(
                    templates.Templates.USER_PROMPT_HISTORY_BLOCK,
                    {
                        templates.TemplateToken.NAME: message.author_name,
                        templates.TemplateToken.MESSAGE: message.body_text
                    }
                )
                message_str += user_sequence_suffix

            message_units = await self._get_unit_count(message_str)
            units_left = self.max_context_units - prompt_units
            if message_units >= units_left:
                context_full = True
                if message_units > units_left:
                    break

            prompt_units += message_units
            history_messages.appendleft(message_str)
            message_count += 1
            author_names.append(message.author_name)

        # then we append the example dialogue, if it exists, and there's room
        # don't add example messages to the message count
        if self.example_dialogue:
            if not context_full and section_separator:
                separator_units = await self._get_unit_count(section_separator)
                context_full = prompt_units + separator_units >= self.max_context_units
            if not context_full:
                remaining_messages = self.history_messages - message_count

                if remaining_messages > 0:
                    if section_separator:
                        # Append the section separator to the start
                        prompt_units += separator_units
                        history_messages.appendleft(section_separator)
                    # Split example dialogue into lines by "real" newlines. The
                    # default sequence suffixes contain newlines and if templated
                    # properly, the example dialogue should be formatted correctly.
                    example_dialogue_lines = self.example_dialogue.split("\n")
                    example_dialogue_messages: typing.List[str] = []
                    for line in example_dialogue_lines:
                        example_dialogue_messages.append(
                            line.format(**self.example_dialogue_template_tokens)
                        )

                    # Fill remaining quota of history messages with example dialogue
                    # messages. This has the effect of gradually pushing them out
                    # with each requst as the chat exceeds the history limit or the
                    # model's context length.
                    while example_dialogue_messages and remaining_messages:
                        # Start from the end of the list since the order is reversed
                        example_message = example_dialogue_messages.pop()
                        # See if it can fit in the context
                        example_units = await self._get_unit_count(example_message)
                        if prompt_units + example_units > self.max_context_units:
                            break

                        # Update the prompt statistics and append the example message
                        prompt_units += example_units
                        remaining_messages -= 1
                        history_messages.appendleft(example_message)

        # Populate a list of stop sequences using the display names of the members
        # who posted most recently, up to the history limit. We use a dictionary
        # conversion to de-duplicate instead of checking list membership, as this
        # has constant time complexity vs. linear, and also preserves order.
        author_names_dict: typing.Dict[str, None] = dict.fromkeys(author_names)
        author_names_dict.pop(self.persona.ai_name, None) # remove our own name
        author_names = list(author_names_dict.keys())

        fancy_logger.get().debug(
            "Fit %d messages in prompt.",
            message_count
        )
        unit_type = "tokens" if self.ooba_client.can_get_token_count() else "characters"
        fancy_logger.get().debug(
            f"Total {unit_type} in prompt: %d.  Max {unit_type} allowed: %d. Headroom: %d",
            prompt_units,
            self.max_context_units,
            self.max_context_units - prompt_units
        )

        return "".join(history_messages), author_names

    def _render_prompt(
        self,
        message_history_txt: str,
        system_message: str,
        guild_name: str,
        channel_name: str,
    ) -> str:
        system_sequence_prefix = self.template_store.get(
            templates.Templates.SYSTEM_SEQUENCE_PREFIX
        )
        system_sequence_suffix = self.template_store.get(
            templates.Templates.SYSTEM_SEQUENCE_SUFFIX
        )
        current_datetime = self.get_datetime()
        prompt = self.template_store.format(
            templates.Templates.PROMPT,
            {
                templates.TemplateToken.AI_NAME: self.persona.ai_name,
                templates.TemplateToken.DESCRIPTION: self.persona.description,
                templates.TemplateToken.PERSONALITY: self.persona.personality,
                templates.TemplateToken.SCENARIO: self.persona.scenario,
                templates.TemplateToken.GUILD_NAME: guild_name,
                templates.TemplateToken.CHANNEL_NAME: channel_name,
                templates.TemplateToken.CURRENT_DATETIME: current_datetime,
                templates.TemplateToken.SECTION_SEPARATOR: self.template_store.format(
                    templates.Templates.SECTION_SEPARATOR,
                    {
                        templates.TemplateToken.SYSTEM_SEQUENCE_PREFIX: system_sequence_prefix,
                        templates.TemplateToken.SYSTEM_SEQUENCE_SUFFIX: system_sequence_suffix,
                        templates.TemplateToken.AI_NAME: self.persona.ai_name
                    }
                ),
                templates.TemplateToken.SYSTEM_SEQUENCE_PREFIX: system_sequence_prefix,
                templates.TemplateToken.SYSTEM_SEQUENCE_SUFFIX: system_sequence_suffix,
                templates.TemplateToken.MESSAGE_HISTORY: message_history_txt,
                templates.TemplateToken.SYSTEM_MESSAGE: system_message
            }
        )
        prompt += self.template_store.get(templates.Templates.BOT_SEQUENCE_PREFIX)
        prompt += (
           self.bot_prompt_block.rstrip()
           if self.strip_prompt else self.bot_prompt_block
        )
        return prompt

    def _render_secondary_prompt(
        self,
        system_message: str,
        guild_name: str,
        channel_name: str
    ) -> str:
        system_sequence_prefix = self.template_store.get(
            templates.Templates.SYSTEM_SEQUENCE_PREFIX
        )
        system_sequence_suffix = self.template_store.get(
            templates.Templates.SYSTEM_SEQUENCE_SUFFIX
        )
        current_datetime = self.get_datetime()
        message_str = self.template_store.format(
            templates.Templates.SECONDARY_PROMPT,
            {
                templates.TemplateToken.AI_NAME: self.persona.ai_name,
                templates.TemplateToken.SCENARIO: self.persona.scenario,
                templates.TemplateToken.GUILD_NAME: guild_name,
                templates.TemplateToken.CHANNEL_NAME: channel_name,
                templates.TemplateToken.CURRENT_DATETIME: current_datetime,
                templates.TemplateToken.SECTION_SEPARATOR: self.template_store.format(
                    templates.Templates.SECTION_SEPARATOR,
                    {
                        templates.TemplateToken.SYSTEM_SEQUENCE_PREFIX: system_sequence_prefix,
                        templates.TemplateToken.SYSTEM_SEQUENCE_SUFFIX: system_sequence_suffix,
                        templates.TemplateToken.AI_NAME: self.persona.ai_name
                    }
                ),
                templates.TemplateToken.SYSTEM_SEQUENCE_PREFIX: system_sequence_prefix,
                templates.TemplateToken.SYSTEM_SEQUENCE_SUFFIX: system_sequence_suffix,
                templates.TemplateToken.SYSTEM_MESSAGE: system_message
            }
        )

        return message_str

    def get_datetime(self) -> str:
        datetime_format = self.template_store.get(
            templates.Templates.DATETIME_FORMAT
        )
        tz_str = os.environ.get("TZ")
        if tz_str:
            tz=ZoneInfo(tz_str)
        else:
            tz=datetime.now().astimezone().tzinfo
        return datetime.now(tz=tz).strftime(datetime_format)

    async def generate(
        self,
        message_history: typing.Optional[typing.AsyncIterator[types.GenericMessage]],
        bot_user_id: int,
        user_name: str,
        guild_name: str,
        channel_name: str,
        image_requested: typing.Optional[bool] = None,
        rewrite_request: typing.Optional[str] = None
    ) -> typing.Tuple[str, typing.List[str]]:
        """
        Generate a prompt for the AI to respond to.

        Formats any special requests according to the prompt template.
        
        Available special requests:
        - image_requested=True - Inform the AI that the image generator is processing a request
        - image_requested=False - Inform the AI that the image generator didn't work
        - rewrite_request - Ask the AI to rewrite its last response according to instructions
        """
        message_history_text = ""
        author_names: typing.List[str] = []

        # if image requested and SD is online
        if image_requested:
            special_request = self.template_store.format(
                templates.Templates.PROMPT_IMAGE_COMING,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: user_name
                }
            )
        # if SD is offline and we can't
        elif image_requested is False:
            special_request = self.template_store.format(
                templates.Templates.PROMPT_IMAGE_NOT_COMING,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: user_name
                }
            )
        elif rewrite_request:
            special_request = self.template_store.format(
                templates.Templates.PROMPT_REWRITE_REQUEST,
                {
                    **self.example_dialogue_template_tokens,
                    templates.TemplateToken.USER_NAME: user_name,
                    templates.TemplateToken.INSTRUCTION: rewrite_request
                }
            )
        else:
            special_request = ""

        if message_history:
            message_history_text, author_names = await self._render_history(
                bot_user_id,
                message_history,
                special_request,
                guild_name,
                channel_name
            )

        prompt = self._render_prompt(
            message_history_text,
            special_request,
            guild_name,
            channel_name
        )

        return prompt, author_names
