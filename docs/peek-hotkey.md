# whereami peek — trigger wiring

Peek expands the statusline orientation panel for ~30s in every session
(only the focused window is visible, so global is fine). The trigger is a
file touch — no CLI, no script:

    mkdir -p ~/.claude/whereami && touch ~/.claude/whereami/peek

Re-pressing while open refreshes the window ("hold it open by tapping").
The panel appears within one refresh tick (≤ refreshInterval seconds) and
collapses on its own when the window ages out.

## Raycast (Script Command)

Save as `whereami-peek.sh` in your Raycast script directory, then bind a
hotkey to it in Raycast preferences:

    #!/bin/bash
    # @raycast.schemaVersion 1
    # @raycast.title whereami peek
    # @raycast.mode silent
    mkdir -p ~/.claude/whereami && touch ~/.claude/whereami/peek

## Hammerspoon

Add to `~/.hammerspoon/init.lua` (⌃⌥⌘W as an example chord):

    hs.hotkey.bind({"ctrl", "alt", "cmd"}, "W", function()
      os.execute("mkdir -p ~/.claude/whereami && touch ~/.claude/whereami/peek")
    end)
