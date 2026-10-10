---
name: grids
description: Grid semantics, table geometry, cell actions, filtering, focus and keyboard/drag interaction. Load before building or reviewing a grid, table, row filter or keyboard-driven data widget.
triggers: \bgrid|\bcells?\b|spreadsheet|worksheet|\btables?\b
---
# Editable grids and tables

## Structure
- `role="grid"` with `role="row"`, column and row headers, and `role="gridcell"` cells carrying the names the requirement gives. Names stay stable while scrolling, editing, filtering and selecting.
- Use one layout model so header and body columns line up, and scope component class names so a CSS framework utility cannot change the layout. After changing shared CSS, verify alignment with bounding boxes.

## Interaction
- A cell's own controls sit at its edge and never capture a normal click on the cell.
- A keyboard-driven grid keeps focus on a focusable container so typing, shortcuts and paste reach the selected cell; typing a character starts editing with it.
- Drag selection: press to start, move with the button held to extend, release to end. A right-click inside a selection keeps it, and context-menu commands act on all of it.
- Diagnose a blocked click by checking the element at its coordinates and any open overlay.

## Filtering and data
- Filtered-out rows are removed from display, keyboard navigation and the accessibility tree; their data is unchanged and returns when the filter is cleared.
- Imported content stays as given; only typed input is interpreted.
