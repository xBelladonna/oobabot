# Release v.0.3.3

## What's Changed

This is a release where major refactoring happens throughout the code, changing some design aspects to make it more flexible and scalable. The release also adds some small features and fixes many issues that were missed with the last release. Thanks again to @jmoney7823956789378 for helping to identify the bugs I missed!

This release will be the last one before a major configuration breaking change, where the persona, settings and templates modules will be re-written. This may require manual migration of parts of your config after re-generating it. The purpose of this release is to fit in as many of the fixes and features I can without breaking the config.

Please make sure to regenerate and migrate your config when moving to this release, as some new settings have been added.

You can do this with the `-c config.yml --generate-config` command-line options.

Summary of changes:
- Fixed a _lot_ of bugs
- Rearranged a _lot_ of code
- Proper connection tests in every API client at startup/during operation
- Fixed token counts (again) with (best-effort) consideration of BOS token in token count requests.
- Automatic extension of stop sequences with instruct templates is now configurable:
  - `extend_stop_sequences` config option controls this, and is true by default.
- Implemented a better message queue:
  - New MessageQueue class operates at a per-channel level so activity in one channel isn't affected by activity in another.
  - New response configuration options:
    - `respond_to_latest_only`: if message accumulation is configured, responds once at the end instead of per-message
    - `skip_in_progress_response`: if a new message is received while typing a response, cancel any queued responses and respond to the new message instead
- Option to strip whitespace from the final bot prompt block.
  - `strip_prompt` will strip any whitespace/newlines from the end of the bot prompt block.
- New repetition tracker features:
  - `repetition_threshold` is now configurable. This is the number of times a message must be considered a repetition to automatically hide chat history for the next request.
  - Fuzzy matching, based on token set ratio similarity.
    - `repetition_similarity_threshold` controls how similar messages need to be to be considered repetitions.
- Ignore reactions:
  - A configurable list of reaction emojis and/or custom emoji names (without the surrounding colons) that will hide messages from the AI's context if they appear on messages
  - Messages must be from the AI, or authored by the person reacting, otherwise they are not hidden.
  - The messages remain hidden until the reaction(s) removed.
- Poke by reaction:
  - When reacting to a message with 👆 (not ☝), the bot will be poked with the reacted message as the new message to respond to, unless the message is hidden.
- Handling of attention has been improved:
  - New command `/unpoke`
    - Tells the bot to stop paying attention in the current channel, until summoned again.
    - "Panic" for a configurable duration (15 seconds by default), where all incoming messages will be ignored, no matter what.
      - Can be overridden by using `/poke` or 👆.
  - Unsolicited channel cap now works per-guild, instead of globally. This means the bot can watch up to the unsolicited channel cap in every guild, as well as every DM and group DM.
  - Attention in group DMs is now handled differently
    - Messages in group DMs were considered the same as guild channel messages, in terms of unsolicited responses.
    - In group DMs, it's much more likely that a response would be wanted, even if not directly solicited.
    - If we're past the timeout of our attention span, re-activate our attention and respond, following the response chance curve again as time goes on.
    - This can be overridden by `/unpoke`

### Full Changelog

[All changes from 0.3.2 to 0.3.3](https://github.com/xBelladonna/oobabot/compare/v0.3.2...v0.3.3)