---
name: data-and-persistence
description: Saving changes durably and in order, reloads right after an action, timestamps, undo/redo and filters in URLs. Load before designing the data model or building saving, reload, undo/redo or list filters.
triggers: undo|redo|history|last updated|updated (?:time|at)|\bfilters?\b
---
- A change is durable once the UI shows it: save on every change, without debounce or save-on-unload. The server applies one object's writes in the order they were made, and a reload or navigation right after an action must not lose the write.
- Every view of an object shows the same stored values, timestamps included; UI state saved for convenience (selection, active tab) does not count as a content change.
- Undo/redo follows the requirement's scope; a new change clears redo.
- List filters and views that the user can return to live in the URL, so a reload or shared link restores them.
