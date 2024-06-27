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
        MessageSplitter.END_OF_INPUT to this function. This
        function will then yield any remaining text, even if it
        doesn't look like a full sentence.
        """

        self.full_response += new_token
        unseen = self.full_response[self.printed_idx:]

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
            unseen = self.full_response[self.printed_idx:]


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
        # of the list. Don't print that yet. At the very worst, we'll
        # print it when the END_OF_INPUT signal is reached.
        for sentence_w_char_spans in segments[:-1]:
            # sentence_w_char_spans is a class with the following fields:
            #  - sent: str, sentence text
            #  - start: start idx of 'sent', relative to original string
            #  - end: end idx of 'sent', relative to original string
            # we don't strip newlines or whitespace because we may want that
            # in the output. the calling method should strip them itself.
            to_print = sentence_w_char_spans.sent  # type: ignore
            yield to_print

        # since we've printed all the previous segments,
        # the start of the last segment becomes the starting
        # point for the next round.
        if len(segments) > 0:
            self.printed_idx += segments[-1].start  # type: ignore


class OobaClient(http_client.SerializedHttpClient):
    """
    Client for the Ooba API. Can provide the response by token or by sentence.
    """


    SERVICE_NAME = "Oobabooga"
    OOBABOOGA_STOP_STREAM_URI_PATH = "/v1/internal/stop-generation"
    OOBABOOGA_TOKENIZER_URI_PATH = "/v1/internal/encode"
    OOBABOOGA_TOKEN_COUNT_URI_PATH = "/v1/internal/token-count"
    TABBYAPI_TOKENIZER_URI_PATH = "/v1/token/encode"
    COHERE_TOKENIZER_URI_PATH = "/v1/tokenize"

    def __init__(
        self,
        settings: typing.Dict[str, typing.Any],
        template_store: templates.TemplateStore,
    ):
        self.total_response_tokens = 0
        self.message_regex = settings["message_regex"]
        self.request_params = settings["request_params"]
        self.log_all_the_things = settings["log_all_the_things"]
        self.fetch_token_counts = settings["fetch_token_counts"]
        self.use_chat_completions = settings["use_chat_completions"]
        self.api_type = settings["api_type"].lower()
        if self.api_type not in ("oobabooga", "openai", "tabbyapi", "aphrodite", "cohere"):
            raise ValueError(
                f"Unsupported API type '{self.api_type}'. Please fix your configuration."
            )
        self.model = settings["model"]
        if self.api_type == "cohere" and not self.model:
            raise ValueError(
                "Model is mandatory for the Cohere API. Please fix your configuration."
            )

        # Build base URL and API endpoint from any path component
        base_url: str = settings["base_url"]
        api_endpoint = ""
        match = self.URL_EXTRACTOR.match(base_url)
        if match:
            base_url, path = match.groups()
            if path and path.lstrip("/"):
                api_endpoint += path.rstrip("/")
        # OpenAI-compatible APIs
        if self.api_type in ("oobabooga", "openai", "tabbyapi", "aphrodite"):
            if self.use_chat_completions:
                raise NotImplementedError(
                    "Chat Completions API is not implemented yet. "
                    + "Please use legacy Completions API."
                )
                api_endpoint += "/v1/chat/completions"
            else:
                api_endpoint += "/v1/completions"
        # Cohere is just different
        elif self.api_type == "cohere":
            self.use_chat_completions = False # in case it's left set to true in the config
            api_endpoint += "/v1/chat"

        self.api_endpoint = api_endpoint
        self.api_key = settings["api_key"]
        super().__init__(self.SERVICE_NAME, base_url)

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

    def get_stop_sequences(self) -> typing.List[str]:
        """
        Returns a list of strings that indicate the end of a response.
        Taken from the yaml `stop` within our response_params, and
        includes any system and user beginning of sequence markers.
        """

        stop_sequences = self.request_params.get("stop", [])
        sequence_templates = [
            templates.Templates.SYSTEM_SEQUENCE_PREFIX,
            templates.Templates.SYSTEM_SEQUENCE_SUFFIX,
            templates.Templates.USER_SEQUENCE_PREFIX,
            templates.Templates.USER_SEQUENCE_SUFFIX,
        ]
        for sequence_template in sequence_templates:
            stop_sequence = self.template_store.format(
                sequence_template,
                {},
            ).strip()
            if stop_sequence and stop_sequence not in stop_sequences:
                stop_sequences.append(stop_sequence)

        return stop_sequences

    def can_abort_generation(self) -> bool:
        """
        Indicates if the API type in use supports stopping generation.
        """
        if self.api_type == "oobabooga":
            return True
        return False

    def can_get_token_count(self) -> bool:
        """
        Indicates if the API type in use supports fetching token counts.
        """
        if self.fetch_token_counts and self.api_type in (
            "oobabooga", "tabbyapi", "aphrodite", "cohere"
        ):
            return True
        return False

    async def get_token_count(self, prompt: str) -> int:
        """
        Gets the token count for the given prompt from the Oobabooga internal API.
        """

        if not self.can_get_token_count():
            raise ValueError(
                "API type '%s' does not support tokenization. Cannot get token count."
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "accept": "application/json"
        }

        request = { "text": prompt }

        if self.api_type == "oobabooga":
            url = self.OOBABOOGA_TOKENIZER_URI_PATH
        elif self.api_type in ("tabbyapi", "aphrodite"):
            # Currently the endpoint for both of these is the same
            url = self.TABBYAPI_TOKENIZER_URI_PATH
        elif self.api_type == "cohere":
            url = self.COHERE_TOKENIZER_URI_PATH
            request.update({ "model": self.model })

        # As long as we're working with reasonable amounts of message, it shouldn't take long
        async with self._get_session().post(
            url, headers=headers, json=request, verify_ssl=False
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                raise http_client.OobaHttpClientError(
                    f"Request failed with status {response.status}: {response_text}"
                )
            result = await response.json()

            # Endpoint for aphrodite-engine is the same as tabbyAPI, but not the response
            # schema. Special handling is required.
            if self.api_type == "aphrodite":
                # Added BOS or not depends entirely on the tokenizer loaded. Default is
                # true, not many models change this. Subtract 1 token to account for BOS.
                return int(result.get("content").get("value")) - 1
            # Special handling for the Cohere API as well
            if self.api_type == "cohere":
                return len(result.get("tokens"))

            # BOS token is always added by these APIs, so we remove it.
            # Should always be an int but we cast just in case.
            return int(result.get("length")) - 1

    async def request_by_message(
        self,
        prompt: typing.Union[
            str, typing.List[typing.Dict[str, str]]
        ],
        stop_sequences: typing.List[str] = []
    ) -> typing.AsyncIterator[str]:
        """
        Yields individual messages from the response as it arrives.
        These can be split by a regex or by sentence.
        """
        splitter = self.fn_new_splitter()
        async for new_token in self.request_by_token(prompt, stop_sequences):
            for sentence in splitter.next(new_token):
                yield sentence

    async def request_as_string(
        self,
        prompt: typing.Union[
            str, typing.List[typing.Dict[str, str]]
        ],
        stop_sequences: typing.List[str] = []
    ) -> str:
        """
        Yields the entire response as a single string.
        """
        response = [token async for token in self.request_by_token(prompt, stop_sequences)]
        return "".join(response)

    async def request_as_grouped_tokens(
        self,
        prompt: typing.Union[
            str, typing.List[typing.Dict[str, str]]
        ],
        stop_sequences: typing.List[str],
        interval: float = 0.2,
    ) -> typing.AsyncIterator[str]:
        """
        Yields the response as a series of tokens, grouped by time interval.
        """
        response_iterator = self.request_by_token(prompt, stop_sequences)
        tokens = ""
        last_response = None
        async for token in response_iterator:
            if token == SentenceSplitter.END_OF_INPUT:
                if tokens:
                    yield tokens
                break
            tokens += token
            now = time.perf_counter()
            # Begin counting time after we have actually started receiving tokens,
            # otherwise the initial message will just be the first token only.
            if not last_response:
                last_response = now
            if now < last_response + interval:
                continue
            yield tokens
            tokens = ""
            last_response = time.perf_counter()

    async def request_by_token(
        self,
        prompt: typing.Union[
            str, typing.List[typing.Dict[str, str]]
        ],
        stop_sequences: typing.List[str] = []
    ) -> typing.AsyncIterator[str]:
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
        if self.api_type == "cohere":
            request.update({ "message": prompt })
        elif self.use_chat_completions:
            request.update({ "messages": prompt }) # ensure to pass a list of message objects
        else:
            request.update({ "prompt": prompt })

        request.update(self.request_params)
        # Build list of stop sequences from the oobabooga request_params
        # and our additional runtime-generated sequences, if any
        stop_sequences = self.request_params["stop"] + stop_sequences
        # then if there are any stop sequences at all, we handle API special cases
        if stop_sequences:
            if self.api_type in ("oobabooga", "openai", "tabbyapi"):
                # The real OpenAI Completions and Chat Completions API have a limit
                # of 4 stop sequences
                # TODO: figure out how to properly detect the real OpenAI API
                # to be compatible with things like reverse-proxies that use differnt
                # URL schemata. A head request or similar may be necessary.
                if "api.openai.com" in self.base_url and len(stop_sequences) > 4:
                    fancy_logger.get().debug(
                        "OpenAI in use, truncating to 4 stop sequences as per the API limit."
                    )
                    stop_sequences = stop_sequences[:3] # I'm so glad :3 gets to be valid code
                # We use dict().update() for performance, as there may be hundreds or
                # thousands of them depending on if impersonation prevention is enabled
                # and the channel has many members.
                request.update({ "stop": stop_sequences })
            elif self.api_type == "cohere":
                # The real Cohere Chat API has a limit of 5 stop sequences
                if len(stop_sequences) > 5:
                    fancy_logger.get().debug(
                        "Cohere API in use, truncating to 5 stop sequences as per the API limit."
                    )
                    stop_sequences = stop_sequences[:4] # list-slicing is fast anyway
                request.update({ "stop_sequences": stop_sequences })

            stop_sequences_str = ", ".join(
                [f"'{stop_sequence}'" for stop_sequence in stop_sequences]
            ).replace("\n", "\\n")
            if self.log_all_the_things:
                print(f"Sent request:\n{json.dumps(request, indent=1)}")
                if self.api_type == "cohere":
                    print(
                        "Prompt:\n"
                        + f"{str(request['message']).encode('utf-8', 'replace')}"
                    )
                elif self.use_chat_completions:
                    print(
                        "Messages:\n"
                        + f"{str(request['messages']).encode('utf-8', 'replace')}"
                    )
                else:
                    print(
                        "Prompt:\n"
                        + f"{str(request['prompt']).encode('utf-8', 'replace')}"
                    )
                print("Stop sequences:\n%s", stop_sequences_str)
            else:
                # If we aren't logging all the things, only log stop sequences
                # if we added any at runtime, e.g. from impersonation prevention.
                if stop_sequences:
                    fancy_logger.get().debug(
                        "Using stop sequences: %s", stop_sequences_str
                    )

        async with self._get_session().post(
            self.api_endpoint, headers=headers, json=request, verify_ssl=False
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                raise http_client.OobaHttpClientError(
                    f"Request failed with status {response.status}: {response_text}"
                )
            async for line in response.content:
                finished = False
                decoded_line = line.decode('utf-8').strip()
                if decoded_line.startswith("data: "):
                    decoded_line = decoded_line[6:]  # Strip "data: "
                if not decoded_line:
                    continue
                try:
                    event_data = json.loads(decoded_line)
                except json.JSONDecodeError:
                    fancy_logger.get().debug(
                        "We got an invalid JSON body! Ignoring and moving on."
                    )
                    continue
                if "choices" in event_data:  # Handling the format with "choices"
                    for choice in event_data.get("choices", []):
                        text = choice.get("text", "")
                        if text:
                            self.total_response_tokens += 1
                            if self.log_all_the_things:
                                print(text.encode('utf-8', 'replace'), end="", flush=True)
                            yield text
                        if choice.get("finish_reason"):
                            finished = True
                            break
                else:  # Handling other formats
                    text = event_data.get("text", "")
                    finished = event_data.get("is_finished", False)
                    if text:
                        self.total_response_tokens += 1
                        if self.log_all_the_things:
                            print(text.encode('utf-8', 'replace'), end="", flush=True)
                        yield text
                if finished:
                    break

        # Make sure to signal the end of input
        yield MessageSplitter.END_OF_INPUT

    async def stop(self) -> None:
        """
        Aborts any currently in-progress text generation.
        """
        # New Ooba OpenAI API stopping logic
        url = self.OOBABOOGA_STOP_STREAM_URI_PATH
        headers = {"accept": "application/json"}
        fancy_logger.get().debug("Aborting all ongoing text generation...")
        async with self._get_session().post(url, headers=headers) as response:
            if response.status != 200:
                response_text = await response.text()
                raise http_client.OobaHttpClientError(
                    f"Request failed with status {response.status}: {response_text}"
                )
