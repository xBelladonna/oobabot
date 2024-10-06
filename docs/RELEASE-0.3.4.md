# Release v.0.3.3

## What's Changed

This release introduces a few small features and a set of breaking changes to the configuration that will not be fully compatible with automatic migration. This was part of a code quality cleanup and a restructure to simplify the internal operation and ensure consistency between code and configuration. Some setting names have changed, and Vision API settings will need to be copied over manually. It's recommended to start by migrating your existing configuration with the command-line options `-c config.yml --generate-config` and then copying over settings which did not migrate correctly. A file comparison tool like is available in some IDEs can be useful for this.

Summary of changes:
- Re-optimized message-response pipeline to be fully asynchronous (made it faster, especially at smaller chat history sizes)
- `persona` configuration has been split into 3 new segments:
    - `description`: a short description/summary of your AI
    - `personality`: a description of your AI's personality traits and characteristics
    - `scenario`: the scenario your AI is in, useful for setting up context for interactions
    You can simply place everything in the `description` field if you don't want to separate your prompt into these categories.
- Improved how the message history is assembled
    - Replies are handled more appropriately (future context is masked so the AI focuses on the context of the replied-to message)
    - The starter message of a thread (from the original channel) is now part of the thread history.
    - Automatic lookback for message history will pull messages from the Discord API until we reach our configured message limit, instead of pulling only a fixed number. Since some messages may not be part of the message history (hidden messages, etc), this will ensure we use all headroom and reach our quota.
- The bot will now automatically retry the response generation (up to a configurable limit) if the response returned from the text-generation API was empty.
- The bot will now reply to messages that are behind new messages that arrive while the bot is generating a response (i.e. "buried messages"), so it's clear which message the AI is responding to.
- New configuration option `mention_replied_user` to enable/disable "pinging" (mentioning) a user when replying to them. True by default.
- Added "secondary prompt injection" which allows a system prompt snippet to be injected at a configurable "depth" in the chat history shown to the AI. See `secondary_prompt` in the `templates` section and `secondary_prompt_depth` in the `discord` section of the configuration.
- Some slash commands can now take a `message` parameter which indicates which message in the chat the command should use/operate on.
- Added `/rewrite` command, which allows you to intsruct the AI to rewrite one of its messages (or the latest one if one isn't provided) according to an instruction you provide.
- Added `/status` command, which allows the bot owner or another team member with write permissions to manually set the bot's presence status (online, idle, etc) and/or a custom activity (status text).
- In addition to manual status changes, the bot will now automatically change its status from online to idle once it has been idle for the configured `idle_timeout` (default is 300 seconds). Set to `0` to disable this feature. See `idle_timeout` in the `discord` section of the configuration.
- Added a configurable shortcut to flip the aspect ratio of generated images. See `flip_orientation_param` in `discord` settings. Set to `flip` by default. Use `flip=y` in the image generation prompt (or any other truthy value like `1`, `true`, `yes`, etc) to reverse the default aspect ratio (or swap the width and height provided).

### Full Changelog

[All changes from 0.3.2 to 0.3.3](https://github.com/xBelladonna/oobabot/compare/v0.3.2...v0.3.3)