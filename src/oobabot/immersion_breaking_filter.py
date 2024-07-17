import re
import typing
import pysbd

from oobabot import fancy_logger
from oobabot import prompt_generator
from oobabot import templates

class ImmersionBreakingFilter:
    """
    Filter anything that would break immersion from an AI response.

    These include lines that include a stop marker, lines that attempt
    to carry on the conversation as a different user, and lines that
    include the bot message prompt.
    """

    def __init__(
        self,
        discord_settings: typing.Dict[str, typing.Any],
        prompt_generator: prompt_generator.PromptGenerator,
        template_store: templates.TemplateStore
    ):
        self.prompt_generator = prompt_generator
        self.template_store = template_store
        self.stop_markers = discord_settings["stop_markers"]
        self.use_filter = discord_settings["use_immersion_breaking_filter"]

        # Get a sentence segmenter ready
        self.sentence_splitter = pysbd.Segmenter(language="en", clean=False)
        # and set a regex pattern that we will use to split lines apart. Avoids code
        # duplication in each method where we do this. This must not be a raw string,
        # otherwise the str.strip() method can't use it properly.
        self.line_split_pattern = "\r\n\t\f\v"
        # Compile the above pattern for faster repeated use
        self.line_split_regex = re.compile(r"([" + self.line_split_pattern + r"]+)")
        # Compile a regex to match whitespace commonly used for indentation (e.g. in code)
        self.whitespace_regex = re.compile(r"[\s\t]+")

        # Compile some regex patterns we will use in the immersion-breaking filter to
        # detect if the AI looks like it is continuing the conversation as someone else,
        # or breaking immersion by giving itself a line prefixed with its name.
        name_identifier = "%%%%%%%%NAME%%%%%%%%"
        user_name_pattern = self.template_store.format(
            templates.Templates.USER_PROMPT_HISTORY_BLOCK,
            {
                templates.TemplateToken.NAME: name_identifier,
                templates.TemplateToken.MESSAGE: "",
            },
        ).strip("\n")
        # Discord usernames are 2-32 characters long, and can only contain special
        # characters '_' and '.' but display names are 1-32 characters long and can
        # contain almost anything, so we try to account for the more permissive option.
        # Hopefully results in fewer false positives than matching anything. Using a
        # prompt history block like "[{NAME}]: {MESSAGE}" will work better.
        user_name_pattern = re.escape(user_name_pattern).replace(
            name_identifier, r"[\S ]{1,32}"
        )
        bot_name_pattern = re.escape(
            self.prompt_generator.bot_prompt_block.strip("\n")
        )
        self.user_message_pattern = re.compile(r"^(" + user_name_pattern + r")(.*)$")
        self.bot_message_pattern = re.compile(r"^(" + bot_name_pattern + r")(.*)$")

    def split(self, text: str) -> typing.List[str]:
        """
        Split the provided text into a list of strings by our line split pattern,
        preserving the split characters in the list. This makes it easy to re-join
        them later, without having to guess which character we split at.
        """
        return self.line_split_regex.split(text)

    def segment(self, text: str) -> typing.List[str]:
        """
        Split the provided text into individual sentences using
        pragmatic sentence boundary disambiguation. Each sentence
        includes a trailing space, except for the last one.
        """
        sentences = [
            # Sometimes the trailing space at the end of a sentence is kept,
            # sometimes not. We avoid ambiguity by explicity stripping
            # additional whitespace and re-adding a trailing space.
            x.rstrip(" ") + " " for x in self.sentence_splitter.segment(text)
        ]
        # Remove the trailing space from the last sentence.
        if sentences:
            sentences[-1] = sentences[-1].rstrip(" ")

        return sentences

    def filter(self, text: str) -> typing.Tuple[str, bool]:
        """
        Given a string that represents an individual response message,
        filter any lines that would break immersion.

        These include lines that include a stop marker, lines that attempt
        to carry on the conversation as a different user, and lines that
        include the bot message prompt.

        Returns the subset of the input string that should be sent, and a
        boolean indicating if we should abort the response entirely, ignoring
        any further messages.
        """
        # Do nothing if the filter is disabled
        if not self.use_filter:
            return text, False

        good_lines = []
        abort_response = False

        for line in self.split(text):
            if not line.strip(self.line_split_pattern):
                # If our line is composed of only split characters, just append it to
                # good_lines to preserve them, and move on.
                good_lines.append(line)
                continue

            # Get any leading whitespace (which may constitute code indentation, etc)
            leading_whitespace = self.whitespace_regex.match(line) or ""
            if leading_whitespace:
                leading_whitespace = leading_whitespace.group()

            # Split the line by our pysbd segmenter to get individual sentences
            good_sentences = []
            for sentence in self.segment(line):
                # Check if the bot looks like it's giving itself an extra line
                bot_message_match = self.bot_message_pattern.match(sentence)
                if bot_message_match:
                    # If the display name matches the bot's name, trim the name portion
                    # and keep the remaining text.
                    bot_name_sequence, remaining_text = bot_message_match.groups()
                    bot_prompt_block = self.prompt_generator.bot_prompt_block.strip("\n")
                    if bot_prompt_block in bot_name_sequence:
                        fancy_logger.get().warning(
                            "Caught '%s' in response, trimming '%s' and continuing",
                            sentence, bot_name_sequence
                        )
                        sentence = remaining_text

                # Otherwise, filter out any potential user impersonation
                user_message_match = self.user_message_pattern.match(sentence)
                if user_message_match:
                    # If the display name is not the bot's name and matches the
                    # user name pattern, assume it is a user name and abort the
                    # response for breaking immersion.
                    fancy_logger.get().warning(
                        "Filtered out '%s' from response, aborting", sentence
                    )
                    abort_response = True
                    break # break out of the sentence processing loop

                # Look for stop markers within a sentence
                for marker in self.stop_markers:
                    if marker in sentence:
                        keep_part, removed = sentence.split(marker, 1)
                        fancy_logger.get().warning(
                            "Caught '%s' in response, trimming '%s' and aborting",
                            sentence, removed
                        )
                        if keep_part:
                            good_sentences.append(keep_part)
                        abort_response = True
                        break

                # If we're aborting the response, stop processing additional sentences.
                if abort_response:
                    break

                # filter out sentences that are entirely made of whitespace/newlines
                if sentence.strip():
                    good_sentences.append(sentence)

            # If we're aborting the response, stop processing additional lines
            if abort_response:
                break

            # Re-add any leading whitespace and re-join sentences
            good_line = leading_whitespace + "".join(good_sentences)
            if good_line:
                good_lines.append(good_line)

        # Finally, re-join all good lines and return the new response
        return "".join(good_lines), abort_response
