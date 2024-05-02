
# Release v.0.3.0

Note: version 0.2.2 only updated oobabot-plugin, not oobabot.  This
shows changes to oobabot since the prior release, [v0.2.1](RELEASE-0.2.1.md).

## What's Changed

Since this is a major version release, a *lot*...
* Many bugfixes
* New slash-comands
* Reaction controls
* OpenAI API compatibility
* GPT Vision API support
* Many more templating options (check the example configuration)
  * Instruct prompt formatting
  * Bot responses
  * Example dialogue
  * Guild name
  * Channel name
  * Current date/time (also templatable with strftime format)
  * Chat history blocks (separate for both users and bot)
* Image generator
  * More comprehensive detection of image generation requests
  * Avatar/self-portrait request detection
* Impersonation prevention
* Message accumulation period
* Ignore prefixes
* Configuration option to ignore bots
  * Bots were previously always ignored, but this now allows them to interact. Be careful of infinite loops!
  * Useful for compatibility with bots like PluralKit or Tupperbox
* Improved persona file parsing
  * Contatenate description, persona and scenario fields from character cards for a more comprehensive persona
* Response chance now interpolates linearly between calibration entries
  * There is an additional calibration table for voice calls as well

## New Features

* New slash-commands:
  * `/edit "message"`: Edit the bot's last message with the provided message.
  * `/poke`: Prompt the bot to reply to the most recent message in the chat
* Reaction controls:
  * React to one of the bot's messages with ❌ to delete it.
  * React to one of the bot's messages with 🔁 to regenerate it.
  * React to one of the bot's messages with ⏪ to hide that message and all messages before it from the bot's chat history.
* GPT Vision: the bot can now view images by captioning them with GPT Vision. Check the example config for templating options.
* Image generator:
  * Self-portrait request detection
    If one of the configured avatar phrases is used in the image prompt, the phrase will be substituted with the configured avatar prompt.
* Impersonation prevention:
  This automatically adds the display names of the most recent members in the chat history to the list of stop sequences.
  There are 3 different modes:
    * `standard`: Uses the fully templated user prompt prefix from the user history block.
    * `aggressive`: Uses just the "canonical" user display name (for models that use "narrative voice"). This is the "common sense" transformation of any given name, i.e. using only the first name in capitalized form, removing any emojis, etc.
    * `comprehensive`: Combines both standard and aggressive modes. Keep in mind the sequence limit if you are using OpenAI, as they will be truncated at 4 sequences even if there otherwise would be more.
* Message accumulation period:
  * If configured, the bot will wait for this amount of time after a message is received, and reply to the most recent message in the chat.
  * This is achieved with an internal message queue that buffers incoming messages for the configured amount of time.
  * Usefor for compatibility with bots like PluralKit and Tupperbox, which re-post user messages with Webhooks under different names, deleting the original message.
* Ignore prefixes:
  * Any messages starting with an ignore prefix is hidden from the bot. It will not reply or see these messages in the chat history.
  * Multiple prefixes can be added, e.g. to prevent user commands destined for other bots from being processed.

## Bug Fixes / Tech Improvements

Too many to list, however, none of the identified bugs existed between the last version release and this release.
Please check full changelog for more details.

### Full Changelog

[All changes from 0.2.3 to 0.3.0](https://github.com/xBelladonna/oobabot/compare/v0.2.3...v0.3.0)