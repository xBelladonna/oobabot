# `oobabot`

**`oobabot`** is a Discord bot which talks to Large Language Model AIs (like Llama, Mistral, ChatGPT, etc...), using just about any API-enabled backend.

- [oobabooga's text-generation-webui](https://github.com/oobabooga/text-generation-webui)
- [tabbyAPI](https://github.com/theroyallab/tabbyAPI)
- [aphrodite-engine](https://github.com/PygmalionAI/aphrodite-engine)
- [LocalAI](https://github.com/mudler/LocalAI)
- [vLLM](https://github.com/vllm-project)

It even supports non-local solutions such as Openrouter, Cohere, OpenAI, etc.

**Updated! Use `--generate-config` and update your configs! If you want to migrate your existing config, specify your config file as well: `-c config.yml --generate-config`**


## Installation and Quick Start!
Requires python 3.8+

See [INSTALL.md](./docs/INSTALL.md) for detailed installation instructions.

1. Install LLM backend with an OpenAI-compatible API.
    - Optionally, skip this step and run via a cloud provider!
2. Create a [Discord bot account](https://discordpy.readthedocs.io/en/stable/discord.html), invite it to your server, and note its authentication token.
3. [Install **`oobabot`** (see INSTALL.md)](./docs/INSTALL.md)
4. Generate a configuration file
    ```bash
    $ oobabot --generate-config > config.yml
    ```
5. Edit config.yml with you favorite text editor and fill in all the cool parts.
6. Generate an invite link and click/copy it into your browser.
    ```bash
    $ oobabot --invite-url -c config.yml
    ```
7. Finally, run the bot!
    ```bash
    $ oobabot -c config.yml
    ```
    or if you installed it with Poetry:
    ```bash
    $ poetry run oobabot
    ```

## Features

| **`oobabot`**  | How that's awesome |
|---------------|------------------|
| **User-supplied persona** | You supply the persona on how would like the bot to behave
| **Multiple conversations** | Can track multiple conversational threads, and reply to each in a contextually appropriate way
| **Wakewords** | Can monitor all channels in a server for one or more wakewords or @-mentions
| **Private conversations** | Can chat with you 1:1 in a DM
| **Good Discord hygiene** | Splits messages into independent sentences, pings the author in the first one
| **Low-latency** | Streams the reply live, sentence by sentence, or even by small groups of tokens. Provides lower latency, especially on longer responses.
| **Stats** | Track token generation speed, latency, failures and usage
| **Easy networking** | Connects to discord from your machine using websockets, so no need to expose a server to the internet
| **Stable Diffusion** | Optional image generation with AUTOMATIC1111
| **Slash commands** | Did your bot get confused?  `/lobotomize` it!
| **OpenAI API support** | Roughly supports OpenAI-compatible API endpoints as well as the Cohere API (Command R+)
| **Vision API support** | Roughly supports GPT Vision API (tested with llama-cpp-python API and LocalAI)

You should now be able to run oobabot from wherever pip installed it.
If you're on windows, you should use `python3 -m oobabot (args here)`

There are a **LOT** of settings in the `config.yml`, and it can be tough to figure out what works best.
There is a somewhat populated example config [here](./docs/example-config.yml) for you to inspect and get familiar with. This config is not complete and has no Discord token, so don't try to run the bot with it. Generate a configuration file first, then fill out what you need.

## Optional settings

- **`wakewords`**

  One or more words that the bot will look for. It will reply to any message which contains one of these words, in any channel.

## Persona: the fun setting

**`persona`**

This is a short few sentences describing the role your bot should act as. For instance, this is what you might use for a cat-bot, whose name is "Rosie":

  ```console
  Here is some background information about Rosie:
  - You are Rosie
  - You are a cat
  - Rosie is a female cat
  - Rosie is owned by Chris, whose nickname is xxxxxxx
  - Rosie loves Chris more than anything
  - You are 9 years old
  - You enjoy laying on laps and murder
  - Your personality is both witty and profane
  - The people in this chat room are your friends
  ```

Persona may be set from the command line with the **`--persona`** argument, or within the `config.yml`.

Alternatively, oobabot supports loading Tavern-style JSON character cards!

```yaml
# Path to a file containing a persona. This can be just a single string, a JSON file in
# the common "tavern" formats, or a YAML file in the Oobabooga format. With a single
# string, the persona will be set to that string. Otherwise, the ai_name and persona will
# be overwritten with the values in the file. Also, the wakewords will be extended to
# include the character's own name.
persona_file: 
```

## Then, run it

You should see something like this if everything worked:

![oobabot running!](./docs/oobabot-cli.png "textually interesting image")

---

## Interacting with **`oobabot`**

By default, **`oobabot`** will listen for three types of messages in the servers it's connected to:

- Any message in which **`oobabot`**'s account is @-mentioned
- Replies to **`oobabot`**'s messages
- Any direct message
- Any message containing a provided wakeword (see Optional Settings)

Also, the bot has a random chance of sending follow-up messages in the
same channel if others respond within some time of its last post. This "random chance" is configurable via the `config.yml`:

```yaml
# Time vs. response chance - calibration table
# List of tuples with time in seconds and response chance as float between 0-1
time_vs_response_chance:
  - (180.0, 0.99)
  - (300.0, 0.7)
  - (600.0, 0.5)
```

Here, you can see that NEW messages within 3 minutes of the bot's last reply will have a 99% chance of response.

- Between 3-5 minutes, the default chance drops to 70%
- Between 5-10 minutes, the default chance drops to 50%
- After 10 minutes, the bot stops responding and has to be mentioned again in some way (i.e. ping, reply, wakeword).

Feel free to configure this to suit your needs!


### Reaction controls

As of 0.3.3, the bot now supports reaction controls:
* React to one of the bot's messages with ❌ to delete it.
* React to one of the bot's messages with 🔁 to regenerate it.
* React to one of the bot's messages with ⏪ to hide that message and all messages before it from the bot's chat history.
* React to one of the bot's messages with 👆 (not ☝) to prompt it to respond to the reacted message (unless the message is hidden).
* React to one of the bot's messages, or one of your own messages with 🚫 to hide that message from its chat history. To unhide it, simply remove the reaction.

After the action is complete, the reaction will be automatically removed. Note that the bot cannot remove reactions itself in DMs and Group DMs. This is by design due to Discord channel types and their respective permissions. You must remove the reaction yourself if you are in a DM with the bot.


### Slash Commands

As of 0.3.4, the bot now supports the following slash commands:

| **`/command`**  | What it does |
|---------------|------------------|
| **`/lobotomize`** | Make the bot forget everything in the channel before the command is run
| **`/say "message"`** | Make the bot post the provided message
| **`/edit "message"`** | Make the bot edit a specific message if provided, or its most recent message, with the provided message
| **`/stop`** | Make the bot abort the currently generating message and post what it has so far
| **`/poke`** | Prompt the bot to reply to a specific message if provided, or the latest message
| **`/unpoke`** | Stop paying attention to the channel the command is issued in
| **`/rewrite`** | Make the bot rewrite one of its messages if provided, or most recent message, according to an instruction provided
| **`/status`** | Set the bot's presence status (online, idle, etc) and/or a custom activity (status text). **Note:** Only the bot owner (or a team member with write permissions) may use this command.


Oobabot doesn't add any restrictions on who can run these commands, but luckily Discord does! You can find this inside Discord by visiting "Server Settings" -> Integrations -> Bots and Apps -> hit the icon which looks like [/] next to your bot

If you're running on a large server, you may want to restrict who can run these commands. I suggest creating a new role, and only allowing that role to run the commands.

---

## Stable Diffusion via AUTOMATIC1111

- **`stable-diffusion-url`**

  This is the URL to a server running [AUTOMATIC1111/stable-diffusion-webui](https://github.com/AUTOMATIC1111/stable-diffusion-webui)

  With it, users can ask **`oobabot`** to generate images and post the
  results to the channel. The user who made the original request can
  choose to regenerate the image as they like. If they either don't
  find one they like, or don't do anything within 3 minutes (by default),
  the image will be removed.

  ![oobabot running!](./docs/zombietaytay.png "textually interesting image")

Currently, detection of photo requests is very crude, and is only looking for messages which match specific words/phrases.

The defaults are:
- draw
- sketch
- paint
- make
- generate
- post
- upload

These are configurable in the settings file. As the defaults are rather permissive, experiment with what works best for you in terms of false positives.

Note that depending on the checkpoint loaded in Stable Diffusion, it may not be appropriate for your server's community. I suggest reviewing [Discord's Terms of Service](https://discord.com/terms) and [Community Guidelines](https://discord.com/guidelines) before deciding what checkpoint to run.

**`oobabot`** supports two different negative prompts, depending on whether the channel is marked as "Age-Restricted" or not. This is to allow for more explicit content in channels which are marked as such. While the negative prompt will discourage Stable Diffusion from generating an image which matches the prompt, but is not foolproof.

---

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](./docs/CONTRIBUTING.md) for more information.
