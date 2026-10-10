---
name: forms-and-dialogs
description: Input attributes, form submission and validation, inline editing, dialogs, upload and download. Load before building or reviewing any input, form, error message, dialog or file control.
triggers: \bforms?\b|dialog|modal|validat|invalid|error message|rename|upload|download|confirmation|text ?box|input field
---
# Inputs, forms and dialogs

The requirement defines each control's role, label, value and behaviour; these defaults fill the gaps.

## Inputs
- One visible label per input, linked with `id`/`htmlFor`; help and error text in separate elements referenced by `aria-describedby`.
- The required role decides the element: a textbox is a text input or textarea, `type="number"` is a spinbutton, `type="search"` is a searchbox. Multi-line values use a textarea and keep their line breaks.
- Let invalid input reach the validator: no `maxLength` or native constraints that would hide an application message the requirement asks for.

## Submission
- Follow the scenario's order: opening a form, choosing a mode, filling and submitting are separate actions. Buttons that open or switch use `type="button"`; the submit button and Enter reach the same save logic once.
- Use `noValidate` when the application shows its own messages, and validate on the server.
- Enter submits single-line fields where specified; Escape cancels an edit and restores the saved value.

## Rejection
- A refused submission leaves saved data unchanged everywhere. By default a new-data form keeps the typed text, while an edit field returns to the saved value; the requirement overrides both.
- Mark the field `aria-invalid="true"` and show the exact message in a `role="alert"` element linked to it; clear both after a successful correction.

## Dialogs
- `role="dialog"` with `aria-labelledby` on its heading, one close control, Escape to cancel and focus returned to the trigger. A modal traps focus; a panel the user keeps open while working is non-modal and blocks nothing.
- A confirmation appears before a destructive action, which runs only on confirm.

## Files
- Upload with a labelled native `input type="file"`; download with the required filename and content.
