# Engineering rules for the generated web app

The requirements are a contract verified by end-to-end browser tests. Where the requirement text and these rules differ, the requirement wins.

Work from the supplied public requirements, their referenced assets and the application workspace. Resolve uncertainty from the published text; do not look for private evaluators, reference solutions or platform internals, and do not guess at hidden tests.

## How the app is evaluated
- The evaluator installs and builds the delivered `frontend/` and `backend/`, then starts the backend, which serves the API and the built frontend on one port. Only what the delivery contains runs.
- Each scenario is played through the browser in a fresh, signed-out session starting at the home page. Elements are found by ARIA role, accessible name, label or visible text; results are checked again after reloads and direct visits.
- Locators are strict: one that matches two elements fails, one that matches nothing fails after a timeout.
- All scenarios share one running server and database, so records from earlier scenarios remain.

## Reading requirements
1. An atomic requirement's description is its acceptance rule; its scenarios give concrete data and the order of actions. Play each scenario in the state the previous step leaves (session, page, open dialog).
2. Quoted UI strings are exact: the same characters, case, spacing and punctuation. Placeholders such as `<name>` stand for real values.
3. When the description and a scenario differ, satisfy both. A specific message or behaviour stated for one case applies only where every condition of that case holds; otherwise the general rule applies.
4. Every page and record is reachable through the navigation and permissions the requirements describe, or through the app's normal navigation when they say nothing.
5. In an existing app, keep correct behaviour and layout while making the requested changes. Passing old tests is not proof that old behaviour is right.

## Names, roles and placement
6. Use the role the requirement names: links navigate, buttons act, headings title pages and dialogs. Every interactive element has an accessible name equal to the required label, with nothing else inside it.
7. Associate every form label with its control. Put each control and value in the page region the requirement describes; when the same text appears in several places, its container tells them apart.
8. Reflect state in ARIA (`aria-selected`, `aria-expanded`, `aria-current`, `aria-invalid`, `disabled`).

## Components and interaction
9. Prefer native elements. Composite widgets (combobox, menu, dialog, tabs, grid) follow the WAI-ARIA Authoring Practices as small shared components; the skills describe them.
10. No browser `alert`, `confirm` or `prompt`: the test browser dismisses them. Show messages and confirmations in the page.
11. Every stated entry point of a feature works, and everything is operable by clicking visible elements.
12. The UI reflects a change as soon as it happens: no animations, transitions or deferred rendering that delay the observable state. Keep the template's error reporter in `frontend/src/main.tsx`: check reports page errors through it.

## Data
13. All data, and any UI state the requirements ask to keep, lives in the backend database.
14. A change is saved when the action completes. A reload or direct visit right after an action shows the new state, so writes must reach the server in order and must not be cancelled by navigation.
15. Seed the records the requirements and scenario preconditions name, with their stated attributes, at backend startup on an empty database. Seeding inserts only missing records, keeps existing rows and user changes across restarts, and finishes before the server accepts requests; add no other sample data.

## Validation and errors
16. Validate on the server and implement exactly the validation the requirements state. A refused operation changes nothing.
17. Show the required message next to its control, mark the control invalid and link the message to it. Server errors return JSON, and the UI never stays in a loading state.

## Delivery
18. Keep the template layout: `frontend/` (Vite + React) and `backend/` (Express + SQLite); the backend listens on `PORT` and serves `/api/*` and the built frontend. Add a dependency only when it is small and essential.
19. A fresh checkout works with `cd frontend && npm install && npm run build && cd ../backend && npm install && npm start`.
