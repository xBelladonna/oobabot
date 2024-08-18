import typing
import base64
import io
import re

import aiohttp
from PIL import Image

from oobabot import fancy_logger
from oobabot import http_client
from oobabot import persona
from oobabot import templates

class VisionClient(http_client.SerializedHttpClient):
    """
    Client for the GPT Vision API. Generates image descriptions given a URL
    or base64-encoded image data.
    """

    SERVICE_NAME = "Vision"

    def __init__(
            self,
            settings: typing.Dict[str, typing.Any],
            persona: persona.Persona,
            template_store: templates.TemplateStore,
        ):
        base_url = settings["vision_api_url"]
        self.api_endpoint = "/v1/chat/completions"
        self.api_key = settings["vision_api_key"]
        self.model = settings["vision_model"]
        self.fetch_urls = settings["fetch_urls"]
        self.max_image_size = settings["max_image_size"]
        self.request_params = settings["request_params"]
        self.template_store = template_store
        self.persona = persona

        match = self.URL_EXTRACTOR.match(settings["vision_api_url"])
        if match:
            base_url, api_endpoint = match.groups()
            if api_endpoint and api_endpoint.lstrip("/"):
                self.api_endpoint = api_endpoint

        super().__init__(self.SERVICE_NAME, base_url)

    async def _setup(self):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        async with self._get_session().get(
            "/v1/models", headers=headers, verify_ssl=False
        ) as response:
            response_status = response.status
        if response_status != 200:
            raise http_client.OobaHttpClientError(
                f"Request failed with status {response_status}"
            )

    async def is_image_url(self, url: str) -> bool:
        # Safeguard against fetching URLs if we aren't configured to
        if not self.fetch_urls:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True) as response:
                    if response.headers.get("content-type", "").startswith("image/"):
                        return True
        except aiohttp.ClientError as err:
            fancy_logger.get().error(
                "Vision: unable to fetch URL to determine if the resource is an image. "
                + "Error: %s: %s",
                type(err).__name__, err
            )

        return False

    def preprocess_image(self, image) -> str:
        """
        Converts a file-like object to a base64-encoded JPEG image.
        """
        # Open image from file-like object
        image = Image.open(image)
        # Downsample the image to something our image recognition model can handle, if necessary
        if image.width > self.max_image_size or image.height > self.max_image_size:
            # Resize image using its largest side as the baseline, preserving aspect ratio
            if image.width > image.height:
                height = round(image.height * (self.max_image_size / image.width))
                image = image.resize((self.max_image_size, height), Image.Resampling.LANCZOS)
            else:
                width = round(image.width * (self.max_image_size / image.height))
                image = image.resize((width, self.max_image_size), Image.Resampling.LANCZOS)

        # Convert image to RGB only (JPEG doesn't support transparency)
        image = image.convert("RGB")
        # Dump image to a byte buffer
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=90, optimize=True)
        buffer.seek(0) # Rewind to the start of the buffer
        # Encode and return the image in base64
        return "data:image/jpeg;base64," + base64.b64encode(buffer.read()).decode("utf-8")

    async def get_image_description(self, image: str) -> str:
        """
        Takes a base64-encoded image or URL and returns a description of the image.
        If the response from the API is empty, or if the image is a URL and we're
        not configured to fetch URLs, a ValueError is raised.
        """
        if self.URL_EXTRACTOR.match(image) and not self.fetch_urls:
            raise ValueError("Image is a URL but we're not allowed to fetch URLs.")

        system_prompt = self.template_store.format(
            templates.Templates.VISION_SYSTEM_PROMPT,
            {
                templates.TemplateToken.AI_NAME: self.persona.ai_name,
            },
        )
        instruction = self.template_store.format(
            templates.Templates.VISION_PROMPT, {}
        )
        if system_prompt:
            system_prompt = {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_prompt,
                    },
                ]
            }
        request = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction,
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image,
                    }
                },
            ]
        }

        messages = []
        if system_prompt:
            messages.append(system_prompt)
        messages.append(request)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            **self.request_params
        }

        async with self._get_session().post(
            url=self.api_endpoint, headers=headers, json=payload
        ) as response:
            if response.status != 200:
                response.raise_for_status()
            data = await response.json()
            if data['choices'] and data['choices'][0]['message']['content']:
                description = data['choices'][0]['message']['content']
                return description

        # Finally, raise an exception if we still haven't returned a result
        raise ValueError("Did not receive a valid response from the Vision API.")
