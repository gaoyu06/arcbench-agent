---
name: menus-and-selects
description: Comboboxes, native selects, menus and option pickers: which kind a requirement asks for and the markup of each. Load before building or reviewing any choice of a value, menu or picker.
triggers: combo ?box|drop-?down|native select|\bmenus?\b|menu ?items?|picker|\boptions? (?:list|named)
---
- The requirement's wording decides the kind of control. A "select" or a choice with no further detail is a native `<select>`; options described as clicked items or with the option role make an ARIA combobox or listbox. A plain button is not a combobox.
- Native select: a labelled `<select>` with `<option value="stored-value">Visible text</option>`.
- ARIA combobox: one shared component with a visible label, a `role="combobox"` element showing the current value, `aria-expanded` and `aria-controls` pointing at a `role="listbox"` of `role="option"` elements with `aria-selected`. Support click, arrow keys, Enter and Escape.
- Menu: a button with `aria-haspopup="menu"` and `aria-expanded` opens a labelled `role="menu"` of `role="menuitem"` entries; Escape closes it and returns focus to the button.
- Closed popups are removed from both interaction and the accessibility tree.
