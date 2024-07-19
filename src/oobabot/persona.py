# -*- coding: utf-8 -*-
"""
Retrieves persona data from a variety of formats.
"""

import json
import os
import re
import typing

import ruamel.yaml as ryaml

from oobabot import fancy_logger
from oobabot import templates


class Persona:
    """
    Handles retrieving persona data from a variety of formats
    """

    # list of keys that, depending on the json/yaml schema, might
    # contain the AI's name. Take the first one found, in order.
    NAME_KEYS = ["char_name", "name"]

    # list of keys that, depending on the json/yaml schema, might
    # contain the AI's persona. Take the first one found, in order.
    DESCRIPTION_KEYS = ["description"]
    PERSONALITY_KEYS = ["char_persona", "personality"]
    SCENARIO_KEYS = ["context", "scenario"]

    OOBABOT_AI_NAME_ENV_VAR = "OOBABOT_AI_NAME"
    OOBABOT_PERSONA_ENV_VAR = "OOBABOT_PERSONA"

    def __init__(
        self,
        persona_settings: typing.Dict[str, typing.Any],
        default_ai_name: str
    ) -> None:
        # Clear AI name if it's set to default
        if persona_settings["ai_name"] == default_ai_name:
            persona_settings["ai_name"] = ""
        # Get AI name from environment variable first, or config if not set
        self.ai_name = os.environ.get(
            self.OOBABOT_AI_NAME_ENV_VAR, ""
        ) or persona_settings["ai_name"]
        self.description = ""
        self.personality = ""
        self.scenario = ""
        self.wakewords: typing.List[str] = persona_settings["wakewords"]
        # List of template tokens to substitute with AI name in persona text
        self._templates_ai_name = [
            str(templates.TemplateToken.AI_NAME),
            "{{char}}"
        ]

        # If a persona file is specified, load any values from there
        if "persona_file" in persona_settings:
            filename = persona_settings["persona_file"]
            try:
                self.load_from_file(filename)
            except FileNotFoundError:
                fancy_logger.get().warning(
                    "Could not find persona file: %s",
                    filename,
                )
                return

        # match messages that include any `wakeword`, but not as part of
        # another word
        self.wakeword_patterns = [
            re.compile(rf"\b{wakeword}\b", re.IGNORECASE)
            for wakeword in self.wakewords
        ]

        # Ensure an AI name is configured
        if not self.ai_name:
            if not default_ai_name:
                raise ValueError(
                    "No AI name configured, cannot continue with an empty name!"
                )
            self.ai_name = default_ai_name

        # Load values from config, overwriting previous values
        if persona_settings["description"]:
            self.description = self.substitute(persona_settings["description"])
        if persona_settings["personality"]:
            self.personality = self.substitute(persona_settings["personality"])
        if persona_settings["scenario"]:
            self.scenario = self.substitute(persona_settings["scenario"])

        # If our persona environment variable is provided, load values from it,
        # overwriting any previous values
        env_persona = os.environ.get(self.OOBABOT_PERSONA_ENV_VAR, "")
        if env_persona:
            self.load_from_text(env_persona)

    def contains_wakeword(self, message: str) -> bool:
        for wakeword_pattern in self.wakeword_patterns:
            if wakeword_pattern.search(message):
                return True
        return False

    def substitute(self, text: str) -> str:
        new_text = str(text)
        for template in self._templates_ai_name:
            new_text = new_text.replace(template, self.ai_name)
        return new_text

    def load_from_file(self, filename: str):
        if not filename:
            return

        if filename.endswith(".json"):
            self.load_from_json_file(filename)
            return

        if filename.endswith(".yaml"):
            self.load_from_yaml_file(filename)
            return

        if filename.endswith(".txt"):
            self.load_from_text_file(filename)
            return

        fancy_logger.get().warning(
            "Unknown persona file extension (expected .json, .yaml, or .txt): %s",
            filename
        )

    def load_from_text_file(self, filename: str):
        with open(filename, "r", encoding="utf-8") as file:
            persona = file.read()
        self.load_from_text(persona)

    def load_from_text(self, text: str):
        split_persona: typing.List[str] = re.split(
            r"^(?:\S+(?:\'s)? ?personality|scenario)(?::| [-—]) ?",
            text,
            flags=re.IGNORECASE + re.MULTILINE
        )

        if split_persona[0].strip():
            self.description = self.substitute(split_persona[0])
        if split_persona[1].strip() and len(split_persona) > 1:
            self.personality = self.substitute(split_persona[1])
        if split_persona[2].strip() and len(split_persona) > 2:
            self.scenario = self.substitute(split_persona[2])

    def load_from_json_file(self, filename: str):
        try:
            with open(filename, "r", encoding="utf-8") as file:
                json_data = json.load(file)

        except json.JSONDecodeError as err:
            fancy_logger.get().warning(
                "Could not parse persona file: %s. Cause: %s",
                filename,
                err,
            )
            return
        self.load_from_dict(json_data)

    def load_from_yaml_file(self, filename):
        with open(filename, "r", encoding="utf-8") as file:
            yaml = ryaml.YAML(typ="safe")
            try:
                yaml_settings = yaml.load(file)
            except ryaml.YAMLError as err:
                fancy_logger.get().warning(
                    "Could not parse persona file: %s. Cause: %s",
                    filename,
                    err,
                )
                return
        self.load_from_dict(yaml_settings)

    def load_from_dict(self, json_data: typing.Dict[str, str]):
        if not self.ai_name:
            for name_key in Persona.NAME_KEYS:
                if name_key in json_data and json_data[name_key]:
                    self.ai_name = json_data[name_key]
                    if self.ai_name not in self.wakewords:
                        # Insert our name at the start of the list. The order
                        # doesn't matter but it makes the logs more neat.
                        self.wakewords.insert(0, self.ai_name)
                    break
        for description_key in Persona.DESCRIPTION_KEYS:
            if description_key in json_data and json_data[description_key]:
                self.description = self.substitute(json_data[description_key])
                break
        for personality_key in Persona.PERSONALITY_KEYS:
            if personality_key in json_data and json_data[personality_key]:
                self.personality = self.substitute(json_data[personality_key])
                break
        for scenario_key in Persona.SCENARIO_KEYS:
            if scenario_key in json_data and json_data[scenario_key]:
                self.scenario = self.substitute(json_data[scenario_key])
                break