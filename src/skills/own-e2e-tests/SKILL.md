---
name: own-e2e-tests
description: Steps, helpers and assertions for your own Playwright end-to-end tests. Load before writing or fixing your own e2e tests.
triggers: .
---
- Play each scenario as written: its starting session and page, its actions in order, and the input method it names (drag, typing, right-click, keyboard shortcut). When a requirement lists several input paths, test each one.
- Helpers do what a user does; they never take a shortcut the scenario does not describe.
- Assert form values through their controls (`toHaveValue`, `toBeChecked`) and displayed text in its region. When the same value appears in a view and an editor, tell them apart by role and container.
- After a refused action, assert the message, the invalid state and that the saved data is unchanged, immediately and after a reload.
- Wait for observable results with Playwright's retrying assertions, never with fixed sleeps.
- Diagnose a blocked or misdirected click from the page snapshot and the element at the click point before changing the test.
