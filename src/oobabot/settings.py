# -*- coding: utf-8 -*-
"""
Documents all the settings for the bot. Allows for settings
to be loaded from the environment, command line and config file.

Methods:
    - load:
        loads settings from the environment, command line and config file

    - write_to_stream:
        writes the current config file to the given stream

    - write_to_file:
        writes the current config file to the given file path

Attributes:
    - setting_groups:
        a list of all the setting groups

    - discord_settings:
        a setting group for settings related to Discord

    - oobabooga_settings:
        a setting group for settings related to the oobabooga API

    - stable_diffusion_settings:
        a setting group for settings related to the stable diffusion API

    - general_settings:
        a setting group for settings that are not included in the config file
"""
import os
import shutil
import textwrap
import typing

from oobabot import templates
import oobabot.overengineered_settings_parser as oesp


class SettingsError(Exception):
    """
    Base class for exceptions in this module.
    """

    def __init__(self, message: str, cause: Exception):
        self.message = message
        super().__init__(message, cause)


def _console_wrapped(message):
    width = shutil.get_terminal_size().columns
    return "\n".join(textwrap.wrap(message, width))


def _make_template_comment(
    tokens_desc_tuple: typing.Tuple[typing.List[templates.TemplateToken], str, bool],
) -> list[str]:
    return [
        tokens_desc_tuple[1],
        ".",
        f"Allowed tokens: {', '.join([str(t) for t in tokens_desc_tuple[0]])}",
        "."
    ]


class Settings:
    """
    User=customizable settings for the bot. Reads from
    environment variables and command line arguments.
    """

    ############################################################
    # This section is for constants which are not yet
    # customizable by the user.

    # This is a table of the probability that the bot will respond
    # in an unsolicited manner (i.e. it isn't specifically pinged)
    # to a message, based on how long ago it was pinged in that
    # same channel.
    TIME_VS_RESPONSE_CHANCE: typing.List[typing.Tuple[float, float]] = [
        # (seconds, base % chance of an unsolicited response)
        (180.0, 0.99),
        (300.0, 0.70),
        (60.0 * 10, 0.50)
    ]
    VOICE_TIME_VS_RESPONSE_CHANCE: typing.List[typing.Tuple[float, float]] = [
        # (seconds, base % chance of an unsolicited response)
        (30.0,  0.95),
        (60.0,  0.90),
        (180.0, 0.85)
    ]

    OOBABOOGA_DEFAULT_REQUEST_PARAMS: oesp.SettingDictType = {
        "max_tokens": 300,
        "truncation_length": 4096,
        "auto_max_new_tokens": True,
        "min_length": 0,
        "add_bos_token": True,
        "ban_eos_token": False,
        "skip_special_tokens": True,
        "custom_token_bans": "",
        "logit_bias": {},
        "stop": [],
        "do_sample": True,
        "temperature_last": True,
        "seed": -1,
        "temperature": 0.98,
        "top_p": 1,
        "min_p": 0.05,
        "top_a": 0,
        "top_k": 40,
        "typical_p": 1,
        "tfs": 1,
        "repetition_penalty": 1.18,
        "repetition_penalty_range": 0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "grammar_string": "",
        "guidance_scale": 1,
        "negative_prompt": "",
        "dynamic_temperature": False,
        "dynatemp_low": 0.83,
        "dynatemp_high": 1.3,
        "dynatemp_exponent": 1,
        "epsilon_cutoff": 0,  # In units of 1e-4
        "eta_cutoff": 0,  # In units of 1e-4
        "mirostat_mode": 0,
        "mirostat_tau": 5,
        "mirostat_eta": 0.1,
        "smoothing_factor": 0,
        "encoder_repetition_penalty": 1,
        "no_repeat_ngram_size": 0
    }

    VISION_DEFAULT_REQUEST_PARAMS: oesp.SettingDictType = {
        "max_tokens": 300,
        "truncation_length": 4096,
        "auto_max_new_tokens": True,
        "min_length": 0,
        "add_bos_token": True,
        "ban_eos_token": False,
        "skip_special_tokens": True,
        "stop": [],
        "do_sample": True,
        "temperature_last": True,
        "seed": -1,
        "temperature": 0.2,
        "top_p": 0.95,
        "min_p": 0,
        "top_k": 0,
        "typical_p": 1,
        "tfs": 1,
        "repetition_penalty": 1,
        "repetition_penalty_range": 0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "guidance_scale": 1,
        "negative_prompt": ""
    }

    # set default negative prompts to make it more difficult
    # to create content against the discord TOS
    # https://discord.com/guidelines

    # use this prompt for "age_restricted" Discord channels
    #  i.e. channel.nsfw is true
    DEFAULT_SD_NEGATIVE_PROMPT_NSFW: str = "animal harm, suicide, loli"

    # use this prompt for non-age-restricted channels
    DEFAULT_SD_NEGATIVE_PROMPT: str = DEFAULT_SD_NEGATIVE_PROMPT_NSFW + ", nsfw"

    SD_CLIENT_MAGIC_MODEL_KEY = "model"

    DEFAULT_SD_REQUEST_PARAMS: oesp.SettingDictType = {
        "cfg_scale": 7,
        #    This is a privacy concern for the users of the service.
        #    We don't want to save the generated images anyway, since they
        #    are going to be on Discord. Also, we don't want to use the
        #    disk space.
        "do_not_save_samples": True,
        "do_not_save_grid": True,
        "enable_hr": False,
        # this is a fake setting, SD calls it "sd_model_checkpoint",
        # and it needs to appear under "override_params". But put it here
        # for convenience, the SD client will do the right thing with it.
        SD_CLIENT_MAGIC_MODEL_KEY: "",
        "negative_prompt": DEFAULT_SD_NEGATIVE_PROMPT,
        "negative_prompt_nsfw": DEFAULT_SD_NEGATIVE_PROMPT_NSFW,
        "sampler_name": "",
        "seed": -1,
        "steps": 30,
        "width": 512,
        "height": 512,
        "override_settings": {
            "CLIP_stop_at_last_layers": 1,
            "sd_vae": "Automatic",
            "enable_pnginfo": False
        },
        "override_settings_restore_afterwards": True
    }

    DEFAULT_SD_USER_OVERRIDE_PARAMS = [
        "cfg_scale",
        "enable_hr",
        SD_CLIENT_MAGIC_MODEL_KEY,
        "negative_prompt",
        "sampler_name",
        "seed",
        "height",
        "width"
    ]

    # words to look for in the prompt to indicate that the user
    # wants to generate an image
    DEFAULT_IMAGE_WORDS: typing.List[str] = [
        "draw",
        "sketch",
        "paint",
        "make",
        "generate",
        "post",
        "upload"
    ]

    DEFAULT_AVATAR_WORDS: typing.List[str] = [
        "self-portrait",
        "self portrait",
        "your avatar",
        "your pfp",
        "your profile pic",
        "yourself",
        "you"
    ]

    # ENVIRONMENT VARIABLES ####
    DISCORD_TOKEN_ENV_VAR: str = "DISCORD_TOKEN"
    OOBABOT_PERSONA_ENV_VAR: str = "OOBABOT_PERSONA"

    def __init__(self):
        self._settings = None

        self.arg_parser = None
        self.setting_groups: typing.List[oesp.ConfigSettingGroup] = []

        ###########################################################
        # General Settings
        #  won't be included in the config.yaml

        self.general_settings = oesp.ConfigSettingGroup(
            "General Settings", include_in_yaml=False
        )
        self.setting_groups.append(self.general_settings)

        self.general_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="help",
                default=False,
                description_lines=[],
                cli_args=["-h", "--help"]
            )
        )
        # read a path to a config file from the command line
        self.general_settings.add_setting(
            oesp.ConfigSetting[str](
                name="config",
                default="config.yml",
                description_lines=[
                    textwrap.dedent(
                        """
                        Path to a config file to read settings from.
                        Command line settings will override settings in this file.
                        """
                    )
                ],
                cli_args=["-c", "--config"]
            )
        )
        self.general_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="generate_config",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        If set, oobabot will print its configuration as a .yml file,
                        then exit. Any command-line settings also passed will be
                        reflected in this file.
                        """
                    )
                ]
            )
        )
        self.general_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="invite_url",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Print a URL which can be used to invite the
                        bot to a Discord server. Requires that
                        the Discord token is set.
                        """
                    ),
                ],
                cli_args=["--invite-url"]
            )
        )

        ###########################################################
        # Persona Settings

        self.persona_settings = oesp.ConfigSettingGroup("Persona")
        self.setting_groups.append(self.persona_settings)

        self.persona_settings.add_setting(
            oesp.ConfigSetting[str](
                name="ai_name",
                default="oobabot",
                description_lines=[
                    textwrap.dedent(
                        """
                        Name the AI will use to refer to itself.
                        """
                    )
                ]
            )
        )
        self.persona_settings.add_setting(
            oesp.ConfigSetting[str](
                name="persona",
                default=os.environ.get(self.OOBABOT_PERSONA_ENV_VAR, ""),
                description_lines=[
                    textwrap.dedent(
                        f"""
                        This prefix will be added in front of every user-supplied
                        request. This is useful for setting up a 'character' for the
                        bot to play. Alternatively, this can be set with the
                        {self.OOBABOT_PERSONA_ENV_VAR} environment variable.
                        """
                    ),
                    _make_template_comment(([templates.TemplateToken.AI_NAME], "", True))[2],
                ],
                show_default_in_yaml=False
            )
        )
        # path to a json or txt file containing persona
        self.persona_settings.add_setting(
            oesp.ConfigSetting[str](
                name="persona_file",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        Path to a file containing a persona. This can be just a
                        single string, a JSON file in the common "tavern" formats,
                        or a YAML file in the Oobabooga format.

                        With a single string, the persona will be set to that string.

                        Otherwise, the ai_name and persona will be overwritten with
                        the values in the file. Also, the wakewords will be
                        extended to include the character's own name.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.persona_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="wakewords",
                default=["oobabot"],
                description_lines=[
                    textwrap.dedent(
                        """
                        One or more words that the bot will listen for.
                        The bot will listen in all discord channels it can
                        access for one of these words to be mentioned, then reply
                        to any messages it sees with a matching word.
                        The bot will always reply to @-mentions and
                        direct messages, even if no wakewords are supplied.
                        """
                    )
                ],
                include_in_argparse=False
            )
        )

        ###########################################################
        # Discord Settings

        self.discord_settings = oesp.ConfigSettingGroup("Discord")
        self.setting_groups.append(self.discord_settings)

        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="discord_token",
                default="",
                description_lines=[
                    textwrap.dedent(
                        f"""
                        Token to log into Discord with. For security purposes
                        it's strongly recommended that you set this via the
                        {self.DISCORD_TOKEN_ENV_VAR} environment variable
                        instead, if possible.
                        """
                    )
                ],
                cli_args=["--discord-token"],
                show_default_in_yaml=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="log_level",
                default="DEBUG",
                description_lines=[
                    textwrap.dedent(
                        """
                        Set the log level. Valid values are:
                        CRITICAL, ERROR, WARNING, INFO, DEBUG
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[int](
                name="history_lines",
                default=7,
                description_lines=[
                    textwrap.dedent(
                        """
                        The maximum number of lines of chat history the AI will see
                        when generating a response. The actual number may be smaller
                        than this, due to the model's context length limit.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="allowed_mentions",
                default=[],
                description_lines=[
                    textwrap.dedent(
                        """
                        This is a list of allowed @-mention types. Used to limit what
                        the bot can mention, e.g. to prevent users from tricking the
                        bot into @-mentioning large groups of people and annoying them.
                        """
                    ),
                    "There are 3 possible types:",
                    "  - `everyone`: the @everyone role",
                    "  - `users`: enables @-mentioning users directly",
                    "  - `roles`: enables @-mentioning roles",
                    textwrap.dedent(
                        """
                        If none of these options are selected, only the original author
                        may be @-mentioned.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="reply_in_thread",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        If set, the bot will generate a thread to respond in
                        if it is not already in one.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[float](
                name="message_accumulation_period",
                default=0.0,
                description_lines=[
                    textwrap.dedent(
                        """
                        Time in seconds (rounded to the nearest single decimal, i.e.
                        in increments of 0.1) that the bot will wait for additional
                        messages before deciding to respond. Useful if people post
                        messages shortly after each other, or for use with bots like
                        PluralKit or Tupperbox that proxy your messages as another
                        username and delete the original message.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[int](
                name="continue_on_additional_messages",
                default=0,
                description_lines=[
                    textwrap.dedent(
                        """
                        If this number of messages is added to the message queue
                        while the bot is accumulating messages according to the
                        period above, the bot will stop waiting and immediately
                        begin processing the message queue. A value of 0 means
                        this feature is disabled.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="respond_to_latest_only",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Whether to squash all messages received during the message
                        accumulation period together and respond only once at the
                        end. If set, the bot will respond to the latest message
                        received, otherwise it will respond to each message
                        individually in the order they were received.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="skip_in_progress_responses",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        If the bot receives a new message while in the middle of
                        processing responses, cancel the current message queue
                        and start a new response to the latest message instead.
                        This takes effect regardless of whether the message
                        accumulation period or respond_to_latest_only is set.
                        Image generation requests will not be cancelled.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="stream_responses",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: Stream responses into a single message as
                        they are generated. If not set, the feature is disabled.
                        Note: may be janky
                        """
                    ),
                    "There are 2 options:",
                    textwrap.dedent(
                        """
                        token: Stream responses by groups of tokens, whose size is defined
                        by the stremaing speed limit.
                        """
                    ),
                    textwrap.dedent(
                        """
                        sentence: Stream responses sentence by sentence. Useful if streaming
                        by token is too janky but not splitting responses is too slow. Note:
                        this will cause newlines to be lost, as the responses are returned
                        from the client without newlines.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[float](
                name="stream_responses_speed_limit",
                default=0.7,
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: When streaming responses, cap the
                        rate at which we send updates to Discord to be no
                        more than once per this many seconds.

                        This does not guarantee that updates will be sent this
                        fast, only that they will not be sent any faster than
                        this rate.

                        This is useful because Discord has a rate limit on
                        how often you can send messages, and if you exceed
                        it, the updates will suddenly become slow.

                        Example: 0.2 means we will send updates no faster
                        than 5 times per second.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="dont_split_responses",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Post the entire response as a single message, rather than
                        splitting it into separate messages by sentence.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="ignore_dms",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        If set, the bot will not respond to direct messages.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="ignore_bots",
                default=True,
                description_lines=[
                    textwrap.dedent(
                        """
                        If set, the bot will not respond to other bots' messages.
                        Be careful when disabling this, as your bot may get into
                        infinite loops with other bots.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="ignore_prefixes",
                default=[],
                description_lines=[
                    textwrap.dedent(
                        """
                        This is a list of strings that the bot will ignore if
                        messages begin with any of them. These messages will be
                        hidden from the chat history.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[int](
                name="unsolicited_channel_cap",
                default=3,
                description_lines=[
                    textwrap.dedent(
                        """
                        Adds a limit to the number of channels per guild the
                        bot will post unsolicited responses in at the same
                        time. This is to prevent the bot from being too noisy
                        in large servers.

                        When set, only the most recent N channels the bot has
                        been summoned in will have a chance of receiving an
                        unsolicited response. The bot will still respond to
                        @-mentions and wake words in any channel it can access.

                        Set to 0 to disable this feature.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="time_vs_response_chance",
                default=[str(x) for x in self.TIME_VS_RESPONSE_CHANCE],
                description_lines=[
                    textwrap.dedent(
                        """
                        Time vs. response chance - calibration table.
                        Chance is interpolated according to the time and
                        used to decide the unsolicited response chance.
                        """
                    ),
                    textwrap.dedent(
                        """
                        List of tuples with time in seconds and response
                        chance as float between 0-1.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="voice_time_vs_response_chance",
                default=[str(x) for x in self.VOICE_TIME_VS_RESPONSE_CHANCE],
                description_lines=[
                    textwrap.dedent(
                        """
                        Same calibration table as above but for voice calls. The difference is that
                        we use the last entry's response chance as a fallback instead of refusing
                        to respond after the specified duration, since it's assumed all voice
                        responses are solicited.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[float](
                name="interrobang_bonus",
                default=0.3,
                description_lines=[
                    textwrap.dedent(
                        """
                        How much to increase response chance by if the message ends with ? or !
                        """
                    )
                ],
                include_in_argparse=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="disable_unsolicited_replies",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        If set, the bot will not respond to any messages that
                        do not @-mention it or include a wakeword.

                        If unsolicited responses are disabled, the unsolicited_channel_cap
                        setting will have no effect.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="include_lobotomize_response",
                default=True,
                description_lines=[
                    textwrap.dedent(
                        """
                        Whether or not to include the bot's /lobotomize response
                        in the history following the command.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="use_immersion_breaking_filter",
                default=True,
                description_lines=[
                    textwrap.dedent(
                        """
                        Enable the immersion-breaking filter, which uses several heuristics
                        in order to filter out things like username/display name snippets,
                        and prevent the AI from inventing nonexistent characters and
                        speaking as them.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="stop_markers",
                default=[
                    "<|im_end|>",
                    "<|endoftext|>",
                    "### Instruction:",
                ],
                description_lines=[
                    textwrap.dedent(
                        """
                        A list of strings that will be used to filter out immersion-breaking
                        messages when encountered. The bot looks for these sequences in
                        responses and removes them from the response, using further heuristics
                        to determine if the content following the marker should be removed as
                        well.
                        """
                    ),
                    textwrap.dedent(
                        """
                        Note: this does not stop response generation at the model level!
                        For that, use the "stop" parameter under the oobabooga request_params
                        setting.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="prevent_impersonation",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        Prevent impersonation by automatically adding the display names of
                        the members in the recent message cache (up to the history limit,
                        or 4 sequences if using OpenAI) to the list of stop sequences. If
                        this option is not set, the feature is disabled.
                        """
                    ),
                    "There are 3 options:",
                    textwrap.dedent(
                        """
                        standard: Uses the fully templated user prompt prefix from the
                        user history block.
                        """
                    ),
                    textwrap.dedent(
                        """
                        aggressive: Uses just the "canonical" user display name
                        (for models that use "narrative voice"). This is the "common sense"
                        transformation of any given name, i.e. using only the first name in
                        capitalized form, removing any emojis, etc.
                        """
                    ),
                    textwrap.dedent(
                        """
                        comprehensive: Combines both standard and aggressive modes. Keep
                        in mind the sequence limit if you are using OpenAI, as they will
                        be truncated at 4 sequences even if there otherwise would be more.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[int](
                name="retries",
                default=0,
                description_lines=[
                    textwrap.dedent(
                        """
                        Maximum number of times we will re-query the text generation
                        API to get a response. Useful if the API returns an empty
                        response occasionally or the response was aborted by the
                        immersion-breaking filter.
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[int](
                name="repetition_threshold",
                default=1,
                description_lines=[
                    textwrap.dedent(
                        """
                        Number of times a message is repeated before the repetition tracker
                        kicks in and hides some of the prompt history for the next request
                        to try and reduce the repetition. Disabled if set to 0.
                        """
                    )
                ],
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[float](
                name="repetition_similarity_threshold",
                default=0.0,
                description_lines=[
                    textwrap.dedent(
                        """
                        Fuzzy comparison is performed between the bot's current response
                        and responses logged by the repetition tracker to get a similarity
                        score as a fraction of 1. If the score is equal to or higher than
                        this value, throttle channel history. Fuzzy matching is disabled
                        if set to 0.0, meaning an exact match is required.
                        """
                    )
                ],
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="discrivener_location",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: Path to the Discrivener executable.
                        Will enable prototype voice integration.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )

        self.discord_settings.add_setting(
            oesp.ConfigSetting[str](
                name="discrivener_model_location",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: Path to the Discrivener model to
                        load. Required if discrivener_location is set.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )

        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="speak_voice_replies",
                default=True,
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: Whether to speak responses in voice calls with discrivener
                        """
                    )
                ]
            )
        )
        self.discord_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="post_voice_replies",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        FEATURE PREVIEW: Whether to respond in the voice-text channel of voice calls
                        """
                    )
                ]
            )
        )
        ###########################################################
        # Oobabooga Settings

        self.oobabooga_settings = oesp.ConfigSettingGroup("Oobabooga")
        self.setting_groups.append(self.oobabooga_settings)

        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[str](
                name="base_url",
                default="http://localhost:5000",
                description_lines=[
                    textwrap.dedent(
                        """
                        Base URL for the text generation API. This should be
                        http://hostname[:port] for plain connections, or
                        https://hostname[:port] for connections over TLS.
                        """
                    )
                ]
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[str](
                name="api_key",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        API key for whatever text generation API you are using.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[str](
                name="api_type",
                default="oobabooga",
                description_lines=[
                    textwrap.dedent(
                        """
                        API type for handling different API endpoints.
                        """
                    ),
                    "Currently supported:",
                    "  - oobabooga: Text Generation WebUI",
                    "  - openai: Any generic OpenAI-compatible API - LocalAI, vLLM, etc",
                    "  - tabbyapi: tabbyAPI - OpenAI-compatible exllamav2 API",
                    "  - aphrodite: aphrodite-engine - High-performance backend based on vLLM",
                    "  - cohere: Official Cohere API (Command R+)",
                    textwrap.dedent(
                        """
                        `oobabooga`, `tabbyapi`, `aphrodite` and `cohere` support accurate
                        token counts using their respective API token encoding endpoints.
                        This helps squeeze more context into the available context window.
                        """
                    )
                ]
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="fetch_token_counts",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Some APIs (e.g. mentioned above) support tokenization, which enables
                        accurate token counting. However, this may incur a performance cost
                        depending on your system's performance, or your network latency.
                        If the prompt generation takes too long, you can try disabling this.

                        Note: this checks the token count per-message, so it may push you to
                        an API rate limit very fast or incur large costs if you are using a
                        paid service, so please check with your service before using this!
                        """
                    )
                ]
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="use_chat_completions",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Use the OpenAI Chat Completions API endpoint
                        instead of the legacy Completions API.
                        """
                    )
                ]
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[str](
                name="model",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        Model to use (supported by some endpoints), otherwise leave blank.
                        Example for openrouter: mistralai/mistral-7b-instruct:free
                        """
                    ),
                    "Required for Cohere API."
                ],
                show_default_in_yaml=False
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="log_all_the_things",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Print all AI input and output to STDOUT.
                        """
                    )
                ]
            )
        )
        # get a regex for filtering a message
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[str](
                name="message_regex",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        A regex that will be used to extract message lines
                        from the AI's output. The first capture group will
                        be used as the message. If this is not set, the
                        entire output will be used as the message.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[oesp.SettingDictType](
                name="request_params",
                default=self.OOBABOOGA_DEFAULT_REQUEST_PARAMS,
                description_lines=[
                    textwrap.dedent(
                        """
                        A dictionary which will be passed straight through to
                        Oobabooga on every request. Feel free to add additional
                        simple parameters here as Oobabooga's API evolves.
                        See Oobabooga's documentation for what these parameters
                        mean.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.oobabooga_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="plugin_auto_start",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        When running inside the Oobabooga plugin, automatically
                        connect to Discord when Oobabooga starts. This has no effect
                        when running from the command line.
                        """
                    )
                ],
                include_in_argparse=False
            )
        )

        ###########################################################
        # Vision API Settings
        self.vision_api_settings = oesp.ConfigSettingGroup("Vision API")
        self.setting_groups.append(self.vision_api_settings)

        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[str](
               name="vision_api_url",
               default="",
               description_lines=[
                    textwrap.dedent(
                        """
                        Base URL for the OpenAI-like Vision API. Optionally takes
                        a path component to the Chat Completions endpoint.
                        """
                    )
               ],
               show_default_in_yaml=False
            )
        )
        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[str](
               name="vision_api_key",
               default="",
               description_lines=[
                    textwrap.dedent(
                        """
                        API key for the OpenAI-like Vision API.
                        """
                    )
               ],
               show_default_in_yaml=False
            )
        )
        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[str](
               name="vision_model",
               default="",
               description_lines=[
                    textwrap.dedent(
                        """
                        Model to use for the Vision API.
                        """
                    )
               ],
               show_default_in_yaml=False
            )
        )
        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="fetch_urls",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Fetch images from URLs. Warning: this may lead to your
                        host IP address being leaked to any sites that are accessed!
                        """
                    )
               ]
            )
        )
        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[int](
               name="max_image_size",
               default=1344,
               description_lines=[
                    textwrap.dedent(
                        """
                        Maximum size for the longest side of the image. It will be
                        downsampled to this size if necessary.
                        """
                    )
               ]
            )
        )
        self.vision_api_settings.add_setting(
            oesp.ConfigSetting[oesp.SettingDictType](
                name="request_params",
                default=self.VISION_DEFAULT_REQUEST_PARAMS,
                description_lines=[
                    textwrap.dedent(
                        """
                        A dictionary which will be passed straight through to
                        the Vision API on every request.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        ###########################################################
        # Stable Diffusion Settings

        self.stable_diffusion_settings = oesp.ConfigSettingGroup("Stable Diffusion")
        self.setting_groups.append(self.stable_diffusion_settings)

        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[str](
                name="stable_diffusion_url",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        URL for an AUTOMATIC1111 Stable Diffusion server.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="image_words",
                default=self.DEFAULT_IMAGE_WORDS,
                description_lines=[
                    textwrap.dedent(
                        """
                        When one of these words/phrases is used in a message, the bot will
                        generate an image.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[typing.List[str]](
                name="avatar_words",
                default=self.DEFAULT_AVATAR_WORDS,
                description_lines=[
                    textwrap.dedent(
                        """
                        When one of these words is used in a message, the bot will
                        generate a self-portrait, substituting the avatar word for
                        the configured avatar prompt.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[str](
                name="extra_prompt_text",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        This will be appended to every image generation prompt
                        sent to Stable Diffusion.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[str](
                name="avatar_prompt",
                default="",
                description_lines=[
                    textwrap.dedent(
                        """
                        Prompt to send to Stable Diffusion to generate self-portrait if asked.
                        """
                    )
                ],
                show_default_in_yaml=False
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[bool](
                name="nsfw_dms",
                default=False,
                description_lines=[
                    textwrap.dedent(
                        """
                        Whether to mark DMs and group DMs as NSFW,
                        since this can't be detected automatically.
                        """
                    )
                ]
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[float](
                name="timeout",
                default=180.0,
                description_lines=[
                    textwrap.dedent(
                        """
                        Time in seconds that the generated image will be displayed without
                        interaction before being deleted. Timeout is disabled if set to 0.
                        """
                    )
                ]
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[oesp.SettingDictType](
                name="request_params",
                default=self.DEFAULT_SD_REQUEST_PARAMS,
                description_lines=[
                    textwrap.dedent(
                        """
                        A dictionary which will be passed straight through to
                        Stable Diffusion on every request. Feel free to add additional
                        simple parameters here as Stable Diffusion's API evolves.
                        See Stable Diffusion's documentation for what these parameters
                        mean.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )
        self.stable_diffusion_settings.add_setting(
            oesp.ConfigSetting[list](
                name="user_override_params",
                default=self.DEFAULT_SD_USER_OVERRIDE_PARAMS,
                description_lines=[
                    textwrap.dedent(
                        """
                        These parameters can be overridden by the Discord user
                        by including them in their image generation request.

                        The format for this is: param_name=value

                        This is a whitelist of parameters that can be overridden.
                        They must be simple parameters (strings, numbers, booleans),
                        and they must be in the request_params dictionary.

                        The value the user inputs will be checked against the type
                        from the request_params dictionary, and if it doesn't match,
                        the default value will be used instead.

                        Otherwise, this value will be passed through to Stable
                        Diffusion without any changes, so be mindful of what you allow
                        here. It could potentially be used to inject malicious
                        values into your SD server.

                        For example, steps=1000000 could be bad for your server, unless
                        you have a dozen NVIDIA H100s.
                        """
                    )
                ],
                include_in_argparse=False,
                show_default_in_yaml=False,
                place_default_in_yaml=True
            )
        )

        ###########################################################
        # Template Settings

        self.template_settings = oesp.ConfigSettingGroup(
            "Template",
            description="UI and AI request templates",
            include_in_argparse=False,
        )
        self.setting_groups.append(self.template_settings)

        for template, tokens_desc_tuple in templates.TemplateStore.TEMPLATES.items():
            _, _, is_ai_prompt = tokens_desc_tuple

            self.template_settings.add_setting(
                oesp.ConfigSetting[str](
                    name=str(template),
                    default=templates.TemplateStore.DEFAULT_TEMPLATES[template],
                    description_lines=_make_template_comment(tokens_desc_tuple),
                    include_in_yaml=is_ai_prompt
                )
            )

    META_INSTRUCTION = (
        "\n\n"
        + "# " * 30
        + textwrap.dedent(
            """
            # Please save this output into ./config.yml
            # edit it to your liking, then run the bot again.
            #
            #  e.g. oobabot --generate-config > config.yml
            #       oobabot
            """
        )
    )

    def write_to_stream(self, out_stream) -> None:
        oesp.write_to_stream(self.setting_groups, out_stream)

    def write_to_file(self, filename: str) -> None:
        oesp.write_to_file(self.setting_groups, filename)

    def _filename_from_args(self, args: typing.List[str]) -> typing.Tuple[str, bool]:
        """
        Get the configuration filename from the command line arguments.
        If none is supplied, return the default.

        Returns a tuple with the file to open, and True if it came
        from the default, rather than a CLI argument.
        """

        # we need to hack this in here because we want to know the filename
        # before we parse the args, so that we can load the config file
        # first and then have the arguments overwrite the config file.
        config_setting = self.general_settings.get_setting("config")
        if args:
            for config_flag in config_setting.cli_args:
                # find the element after config_flag in args
                try:
                    return args[args.index(config_flag) + 1], False
                except (ValueError, IndexError):
                    continue
        return config_setting.default, True

    def load_from_yaml_stream(self, stream: typing.TextIO) -> typing.Optional[str]:
        """
        Load the config from a YAML stream.

        params:
            stream: stream to load the config from

        returns:
            None if the config was loaded successfully, otherwise a string
            containing an error message.
        """
        return oesp.load_from_yaml_stream(stream, setting_groups=self.setting_groups)

    def load(
        self,
        cli_args: typing.List[str],
        config_file: typing.Optional[str] = None,
        running_from_cli: bool = False,
    ) -> None:
        """
        Load the config from the command line arguments and config file.

        params:
            cli_args: list of command line arguments to parse
            config_file: path to the config file to load

        cli_args is intended to be used when running from a standalone
        application, while config_file is intended to be used when
        running from inside another process.

        raises SettingsError if a specific configuration file
        was requested (either by the config_file argument or the CLI),
        but it could not be found.
        """

        is_default = False
        if not config_file:
            config_file, is_default = self._filename_from_args(cli_args)
        raise_if_file_missing = not is_default and running_from_cli

        try:
            self.arg_parser = oesp.load(
                cli_args=cli_args,
                setting_groups=self.setting_groups,
                config_file=config_file,
                raise_if_file_missing=raise_if_file_missing,
            )
        except oesp.ConfigFileMissingError as err:
            # get full path to config_file
            config_file = os.path.abspath(config_file)
            msg = f"Could not load config file at: {config_file}"
            raise SettingsError(msg, err) from err

    def print_help(self):
        """
        Prints CLI usage information to STDOUT.
        """
        if not self.arg_parser:
            raise ValueError("display_help called before load")

        help_str = self.arg_parser.format_help()
        print(help_str)

        print(
            "\n"
            + _console_wrapped(
                (
                    "Additional settings can be set in config.yml. "
                    "Use the --generate-config option to print a new "
                    "copy of this file to STDOUT."
                )
            )
        )
