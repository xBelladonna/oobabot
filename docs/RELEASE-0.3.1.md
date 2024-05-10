
# Release v.0.3.1

## What's Changed

This is a minor release that adds some small features and fixes a few issues that were missed with the last release:
* Re-add `SERVER_MEMBERS` privileged gateway intent. Turns out it was necessary for guild nicknames to show up. Whoops.
* Better timeouts for oobabooga client
  * Previously timeouts would sometimes take up to 5 mins
  * Now uses dynamic timeouts with reasonable values (approx. 10 secs, depending on operation)
* Slight improvement to standard impersonation prevention
  * Uses a partial user prompt block with whitespace and newlines stripped.
* Fixed bug that prevented responses to the first message that started a thread.
* Allow responses to message containing only images without text
* Even more comprehensive detection of image generation requests
  * Multiline matching
  * More special characters allowed
  * Messages with multiple sentences now make it through. This is purely for natural language compatibility, so the prompt is still only extracted from the first sentence and the rest discarded.
* Message accumulation period is now more efficient
  * Option to stop waiting after a configurable number of additional messages have been received
  * See `continue_on_additional_messages` setting in `config.yml`
* Robust message queue implementation
  * Messages now get properly queued and responded to coherently, in order of message received.

### Full Changelog

[All changes from 0.3.0 to 0.3.1](https://github.com/xBelladonna/oobabot/compare/v0.3.0...v0.3.1)