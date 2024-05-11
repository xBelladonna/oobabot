# Release v.0.3.2

## What's Changed

This is a minor release that adds some small features and fixes a few issues that were missed with the last release. Thanks to @jmoney7823956789378 for helping to identify the bugs I didn't!
* Fixed a bug where an unhandled exception would occur when using the Cohere API with `log_all_the_things` enabled. This would interrupt the process and stop the bot from working.
* Fixed a bug where a previous factor inverted the boolean logic (again) for `include_lobotomize_response` causing it to behave in the opposite manner to what was expected.
* Fixed a bug where some string-type configuration options were case-sensitive even though it wasn't relevant. The bot will now run properly even if the case doesn't match the valid values.
* Adjustments to the best-effort immersion-breaking filter to hopefully be more universally applicable while minimizing false positives. Turns out it's difficult to make regex cover an infinite range of possibilities.
  * In order to give the bot more structure to determine what is and isn't immersion-breaking, it's recommended to define `history_prompt_block`s that clearly demarcate names from message content.
  * E.g. a history prompt block that looks like `{USER_NAME}: {MESSAGE}` is ambiguous because anything 32 characters or less could be considered a name, which means short sentences with colons in them would be problematic, i.e. "Let me just say: AI is one hell of a trip" would get interpreted as someone with the name "Let me just say" and be filtered out, likely aborting the response.
  * If you use a prompt history block that looks like `[{USER_NAME}]: {MESSAGE}`, this becomes less ambiguous, as the AI ~~will~~ should surround all names within square brackets and thus, regular english sentences wouldn't get caught.
  * What works best depends on the model you're using, so feel free to experiment with different schemata!
* In addition to the above fixes, a new configuration option has been added to disable the immersion-breaking filter entirely. This may be useful if you find that it causes more problems than it's worth and are willing to accept the possibility of immersion-breaking over the inconvenience the filter causes.
  * See `use_immersion_breaking_filter` under the Discord section in the example configuration for more details.
* Responses that exceed the Discord character limit are now handled a little more gracefully. Instead of simply cutting the response short, as before, the bot will now accumulate message contents until the limit is reached, post that message, and then continue in a new message. It will do this until it posts the entire response. Do be careful though! This means if you have a model capable of producing extremely long responses, you could end up with a very crowded channel quite quickly.

### Full Changelog

[All changes from 0.3.1 to 0.3.2](https://github.com/xBelladonna/oobabot/compare/v0.3.1...v0.3.2)