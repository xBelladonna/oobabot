# -*- coding: utf-8 -*-
"""
Client for the Ooba API.
Can provide the response by token or by sentence.
"""
import abc
import json
import re
import time
import typing

import aiohttp
import pysbd
import pysbd.utils

from oobabot import fancy_logger
from oobabot import http_client
from oobabot import templates


class MessageSplitter(abc.ABC):
    """
    Split a response into separate messages.
    """

    # anything that can't be in a real response
    END_OF_INPUT = ""

    def __init__(self):
        self.printed_idx = 0
        self.full_response = ""

    def next(self, new_token: str) -> typing.Generator[str, None, None]:
        """
        Collects tokens into a single string, splits into messages
        by the subclass's logic, then yields each message as soon
        as it's found.

        Parameters:
            new_token: str, the next token to add to the string

        Returns:
            Generator[str, None, None], yields each sentence

        Note:
        When there is no longer any input, the caller must pass
        MessageSplitter.END_OF_INPUT to this function.  This
        function will then yield any remaining text, even if it
        doesn't look like a full sentence.
        """

        self.full_response += new_token
        unseen = self.full_response[self.printed_idx :]

        # if we've reached the end of input, yield it all,
        # even if we don't think it's a full sentence.
        if self.END_OF_INPUT == new_token:
            to_print = unseen.strip()
            if to_print:
                yield unseen
            self.printed_idx += len(unseen)
            return

        yield from self.partition(unseen)

    @abc.abstractmethod
    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        pass


class RegexSplitter(MessageSplitter):
    """
    Split a response into separate messages using a regex.
    """

    def __init__(self, regex: str):
        super().__init__()
        self.pattern = re.compile(regex)

    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        while True:
            match = self.pattern.match(unseen)
            if not match:
                break
            to_print = match.group(1)
            yield to_print
            self.printed_idx += match.end()
            unseen = self.full_response[self.printed_idx :]


class SentenceSplitter(MessageSplitter):
    """
    Split a response into separate messages using English
    sentence word breaks.
    """

    def __init__(self):
        super().__init__()
        self.segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)

    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        segments: typing.List[pysbd.utils.TextSpan] = self.segmenter.segment(
            unseen
        )  # type: ignore -- type is determined by char_span=True above

        # any remaining non-sentence things will be in the last element
        # of the list.  Don't print that yet.  At the very worst, we'll
        # print it when the END_OF_INPUT signal is reached.
        for sentence_w_char_spans in segments[:-1]:
            # sentence_w_char_spans is a class with the following fields:
            #  - sent: str, sentence text
            #  - start: start idx of 'sent', relative to original string
            #  - end: end idx of 'sent', relative to original string
            #
            # we want to remove the last '\n' if there is one.
            # we do want to include any other whitespace, though.

            to_print = sentence_w_char_spans.sent  # type: ignore
            # if to_print.endswith("\n"):
            #     to_print = to_print[:-1]

            yield to_print

        # since we've printed all the previous segments,
        # the start of the last segment becomes the starting
        # point for the next round.
        if len(segments) > 0:
            self.printed_idx += segments[-1].start  # type: ignore


class OobaClient(http_client.SerializedHttpClient):
    """
    Client for the Ooba API.  Can provide the response by token or by sentence.
    """


    SERVICE_NAME = "Oobabooga"
    OOBABOOGA_STOP_STREAM_URI_PATH: str = "/v1/internal/stop-generation"
    OOBABOOGA_TOKENIZER_URI_PATH: str = "/v1/internal/encode"

    def __init__(
        self,
        settings: typing.Dict[str, typing.Any],
        template_store: templates.TemplateStore,
    ):
        super().__init__(self.SERVICE_NAME, settings["base_url"])
        self.total_response_tokens = 0
        self.retries = settings["retries"]
        if self.retries < 0:
            raise ValueError("Number of retries can't be negative. Please fix your configuration.")
        self.message_regex = settings["message_regex"]
        self.request_params = settings["request_params"]
        self.log_all_the_things = settings["log_all_the_things"]
        self.use_generic_openai = settings["use_generic_openai"]
        self.base_url = settings["base_url"]
        self.api_endpoint = "/v1/completions" if not settings["use_chat_completions"] else "/v1/chat/completions"
        self.model = settings["model"]
        self.api_key = settings["api_key"]
        if self.message_regex:
            self.fn_new_splitter = lambda: RegexSplitter(self.message_regex)
        else:
            self.fn_new_splitter = SentenceSplitter
        self.template_store = template_store

    def on_ready(self):
        """
        Called when the client is ready to start.
        Used to log our configuration.
        """
        if self.message_regex:
            fancy_logger.get().debug(
                "Ooba Client: Splitting responses into messages " + "with: %s",
                self.message_regex,
            )
        else:
            fancy_logger.get().debug(
                "Ooba Client: Splitting responses into messages "
                + "by English sentence.",
            )

    async def _setup(self):
        return
    async def __aenter__(self):
        return self

    def get_stopping_strings(self) -> typing.List[str]:
        """
        Returns a list of strings that indicate the end of a response.
        Taken from the yaml `stopping_strings` within our
        response_params.
        """

        stopping_strings = self.request_params.get("stop", [])
        sequence_templates = [
            templates.Templates.SYSTEM_SEQUENCE_PREFIX,
            templates.Templates.SYSTEM_SEQUENCE_SUFFIX,
            templates.Templates.USER_SEQUENCE_PREFIX,
            templates.Templates.USER_SEQUENCE_SUFFIX,
        ]
        for sequence_template in sequence_templates:
            stopping_string = self.template_store.format(
                sequence_template,
                {},
            ).strip()
            if stopping_string and stopping_string not in stopping_strings:
                stopping_strings.append(stopping_string)

        return stopping_strings

    async def get_token_count(self, prompt: str) -> int:
        """
        Gets the token count for the given prompt from the Oobabooga internal API.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

        request = { "text": prompt }

        url = self.base_url + self.OOBABOOGA_TOKENIZER_URI_PATH
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=request, verify_ssl=False) as response:
                if response.status != 200:
                    response_text = await response.text()
                    raise http_client.OobaHttpClientError(
                        f"Request failed with status {response.status}: {response_text}"
                    )
                result = await response.json()
                return result.get("length") # should always be an int

    async def request_by_message(self, prompt: str, stopping_strings: typing.List[str]) -> typing.AsyncIterator[str]:
        """
        Yields individual messages from the response as it arrives.
        These can be split by a regex or by sentence.
        """
        splitter = self.fn_new_splitter()
        async for new_token in self.request_by_token(prompt, stopping_strings):
            for sentence in splitter.next(new_token):
                # remove "### Assistant: " from strings
                if sentence.startswith("### Assistant: "):
                    sentence = sentence[len("### Assistant: "):]
                yield sentence

    async def request_as_string(self, prompt: str, stopping_strings: typing.List[str]) -> str:
        """
        Yields the entire response as a single string, retrying the configured number of times
        if a response isn't received.
        """
        for _tries in range(self.retries + 1): # add offset of 1 as range() is zero-indexed
            response = [token async for token in self.request_by_token(prompt, stopping_strings)]
            if response:
                break
            fancy_logger.get().debug("Empty response received from text generation API! Trying again...")
        return "".join(response)

    async def request_as_grouped_tokens(
        self,
        prompt: str,
        stopping_strings: typing.List[str],
        interval: float = 0.2,
    ) -> typing.AsyncIterator[str]:
        """
        Yields the response as a series of tokens, grouped by time.
        """

        last_response = time.perf_counter()
        tokens = ""
        async for token in self.request_by_token(prompt, stopping_strings):
            if token == SentenceSplitter.END_OF_INPUT:
                if tokens:
                    yield tokens
                break
            tokens += token
            now = time.perf_counter()
            if now < (last_response + interval):
                continue
            yield tokens
            tokens = ""
            last_response = time.perf_counter()

    async def stop(self):
        # New Ooba OpenAPI stopping logic
        async with aiohttp.ClientSession() as session:
            url = self.base_url + self.OOBABOOGA_STOP_STREAM_URI_PATH
            headers = {"accept": "application/json"}

            async with session.post(url, data=json.dumps({}), headers=headers) as response:
                response_text = await response.text()
                print(response_text)
                return response_text

    async def request_by_token(self, prompt: str, stopping_strings: typing.List[str]) -> typing.AsyncIterator[str]:
        """
        Yields the response from the API token by token as it arrives.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

        request = {
            "model": self.model,
            "stream": True,
        }
        # Special handling for Cohere API, which takes "message" instead of "prompt"
        if "api.cohere.ai" in self.base_url:
            request.update({ "message": prompt })
        else:
            request.update({ "prompt": prompt })

        request.update(self.request_params)
        # and then add our additional runtime-generated stopping strings, if any
        if stopping_strings:
            # we use dict().update() for performance
            request.update({ "stop": self.request_params["stop"] + stopping_strings })

        # The real OpenAI Completions and Chat Completions API have a limit of 4 stop sequences
        if "api.openai.com" in self.base_url and len(request["stop"]) > 4:
            request["stop"] = request["stop"][:3] # list-slicing is fast anyway
            fancy_logger.get().debug("Real OpenAI API in use, truncating to 4 stop sequences as per the API limit.")

        fancy_logger.get().debug("Using stop sequences: %s", ", ".join(request["stop"]).replace("\n", "\\n"))

        url = self.base_url + self.api_endpoint
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=request, verify_ssl=False) as response:
                if response.status != 200:
                    response_text = await response.text()
                    raise http_client.OobaHttpClientError(
                        f"Request failed with status {response.status}: {response_text}"
                    )
                if self.log_all_the_things:
                    try:
                        print(f"Sent request:\n{json.dumps(request, indent=1)}")
                        print(f"Prompt:\n{str(request['prompt'])}")
                    except UnicodeEncodeError:
                        print(
                            "Sent request:\n"
                            + f"{json.dumps(request, indent=1).encode('utf-8')}"
                        )
                        print(f"Prompt:\n{str(request['prompt']).encode('utf-8')}")
                async for line in response.content:
                    decoded_line = line.decode('utf-8').strip()
                    if decoded_line.startswith("data: "):
                        decoded_line = decoded_line[6:]  # Strip "data: "
                    if decoded_line:
                        try:
                            event_data = json.loads(decoded_line)
                            if "choices" in event_data:  # Handling the format with "choices"
                                for choice in event_data.get("choices", []):
                                    text = choice.get("text", "")
                                    if text:
                                        self.total_response_tokens += 1
                                        if self.log_all_the_things:
                                            try:
                                                print(text, end="", flush=True)
                                            except UnicodeEncodeError:
                                                print(text.encode("utf-8"), end="", flush=True)
                                        yield text
                                    if choice.get("finish_reason") is not None:
                                        break
                            else:  # Handling other formats
                                text = event_data.get("text", "")
                                is_finished = event_data.get("is_finished", False)
                                if text:
                                    if self.log_all_the_things:
                                        try:
                                            print(text, end="", flush=True)
                                        except UnicodeEncodeError:
                                            print(text.encode("utf-8"), end="", flush=True)
                                    yield text
                                if is_finished:
                                    break
                        except json.JSONDecodeError:
                            continue

                # Make sure to signal the end of input
                yield MessageSplitter.END_OF_INPUT
