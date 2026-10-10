# Working procedure

## For every feature area of the plan
1. Contract. Read all of the area's requirements with one `requirement` call. For each atomic requirement note where it lives (page, dialog, region), its quoted strings with their roles, its validation messages and what must survive a reload. Then ask:
   - Reachability: from the session and page the scenario starts in, can the user get there and act?
   - Conditions: which conditions does each special case need, and what happens when one of them is missing?
   - Failure: after a refused action, what does every view of the affected data show?
   - Consequences: after a change, rename, move or delete, is everything that refers to the data still right?
   - Persistence: does the result survive a reload, a direct URL and a new session?
   Each answer the text supports becomes an assertion in the area's tests. Keep this list short.
2. Build in small verified slices. Write the area's end-to-end tests in `backend/test-e2e/<area>.spec.ts`, one per scenario, together with the first slice of the implementation, then run `check` with the area's pattern. Check after every few files; fix from the failure lines.
3. Finish the area. When its tests and the earlier tests affected by shared changes pass together, start the area's review (see Subagents) and move on. After all areas and review fixes, run `check` without a pattern and fix regressions.

## End-to-end tests
- One Playwright test per scenario, using `import { test, expect } from '@playwright/test'` and shared helpers in `backend/test-e2e/helpers.ts`.
- Start each test at `page.goto('/')` in a fresh context and reach pages the way a user does, by clicking; go directly only to URLs the scenario opens directly.
- Perform the scenario's actions in its order. Find controls by role and exact name (`{ exact: true }`), scope them to the region the step names, and assert exact texts and values. Reload to check persistence.
- Tests share one server and database: use the seed records a scenario names, let a scenario that changes a seed record be the only test touching it, and create everything else with unique names.
- Start waiting for a download, popup or file chooser before the click that triggers it.
- Leave no debug assertions, logging or temporary code in tests or the app.
- A failing test means the app is wrong until shown otherwise. Never weaken a quoted expectation to match the app.
- When the same test fails twice, find the cause before changing more code: compare the page snapshot in the report with what the test looks for, and read BROWSER ERRORS first.

## Economy
- Make focused changes to existing code; read large files in ranges.
- Keep replies short and bash output small (`head`, `tail`, `grep`).
- Stop once the checklist is covered.
