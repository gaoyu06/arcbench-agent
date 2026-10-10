---
name: web-ui-basics
description: Rules every web UI follows: placement, headings, navigation that renders before data, URLs and API caching. Load before building any UI.
triggers: .
---
- Each named control has the role, name and placement the requirement states; opening a form or picker keeps the rest of the layout in place.
- User text is shown exactly as entered.
- Each page has one main heading naming what it shows; secondary details go in other elements.
- Navigation (header, tabs, sidebar, action buttons) renders from the URL and the session on the first paint; only lists and details wait for data.
- Every URL works on direct open and reload; unknown paths fall back to home. API responses are not cached (`Cache-Control: no-store`).
