---
name: clipboard
description: Copy, cut and paste that work when the browser denies clipboard permissions. Load before building copy, cut or paste.
triggers: \bcopy|\bcut\b|paste|clipboard
---
- Handle the real `copy`, `cut` and `paste` events and exchange plain text through `clipboardData` (tab-separated columns, newline-separated rows). Cut followed by paste moves the data.
- Assume `navigator.clipboard` may be denied. Keep an internal clip of the last copy or cut and use it when the event carries no data; use `navigator.clipboard` only as an extra path.
- Every entry point (keyboard shortcut, context menu, toolbar) reaches the same logic.
- Validate a paste before applying it; a refused paste changes nothing.
