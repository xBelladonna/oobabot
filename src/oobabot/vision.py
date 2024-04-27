import typing
import base64
import io
import re
import requests
import aiohttp
from PIL import Image

from oobabot import persona
from oobabot import templates

class VisionClient:
    """
    Client for the GPT Vision API. Generates image descriptions given a URL
    or base64-encoded image data.
    """

    def __init__(
            self,
            settings: typing.Dict[str, typing.Any],
            persona: persona.Persona,
            template_store: templates.TemplateStore,
        ):

        self.fetch_urls = settings["fetch_urls"]
        self.api_url = settings["vision_api_url"]
        self.api_key = settings["vision_api_key"]
        self.model = settings["vision_model"]
        self.max_tokens = settings["max_tokens"]
        self.max_image_size = settings["max_image_size"]
        self.template_store = template_store
        self.persona = persona

        self.url_extractor = re.compile(r"(https?://\S+)")

    def preprocess_image(self, image: Image.Image) -> str:
        """
        Converts a PIL Image object to a base64-encoded JPEG image.
        """
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

    async def get_image_description(self, image: str) -> typing.Optional[str]:
        """
        Takes a base64-encoded image or URL and returns either a description
        of the image, or None if the API returns an empty response.
        """
        if self.url_extractor.match(image):
            if self.fetch_urls:
                r = requests.head(image, allow_redirects=True, timeout=10)
                if not r.headers["content-type"].startswith("image/"):
                    return
            else:
                return

        system_prompt = self.template_store.format(
            templates.Templates.GPT_VISION_SYSTEM_PROMPT,
            {
                templates.TemplateToken.AI_NAME: self.persona.ai_name,
            },
        )
        instruction = self.template_store.format(
            templates.Templates.GPT_VISION_PROMPT,
            {},
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
            "max_tokens": self.max_tokens
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url=self.api_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data['choices'] and data['choices'][0]['message']['content']:
                        description = data['choices'][0]['message']['content']
                        return description
                    else: return
                else:
                    response.raise_for_status()
