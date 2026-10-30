# Desktop design direction

## Direction

Dark research console: make the results surface feel like a focused instrument for finding and reviewing sources. Use near-black charcoal surfaces, crisp monospace typography, and a single restrained lime accent. The results table and selected-resource detail carry the hierarchy; rules and grid lines stay quiet and functional.

## Visual rules

- Use `#0B0C0A` and `#111713` for the base surfaces, high-contrast text, and `#C6FF4A` as the only strong accent. Keep borders flat and 1px; avoid gradients, shadows, and rounded cards.
- Use Cascadia Mono with Consolas and generic monospace fallbacks. Keep the page title clear but compact so the search and review content remain primary.
- Align search controls, saved-search selection, result rows, and detail fields to a consistent column rhythm. Keep the result list and selected-resource evidence visible together at desktop widths.
- Use restrained rules and spacing instead of card-heavy decoration. Distinguish papers, datasets, loading, partial-provider results, empty results, and failures through clear text and accessible state styling.
- At narrow window sizes, let the result list and detail pane stack or switch through a splitter without hiding essential search or saved-history actions.
- Keep source links visibly identifiable and show their provider in the detail view. Theme document text and links to match the application surface. Preserve unknown metadata as unknown. Never use visual styling to imply verification, open access, or relevance that the data does not establish.

## Interaction rules

- The topic field and search action lead the page. Dataset mode makes the fixed Harvard Dataverse source clear; paper mode exposes the supported provider choices.
- Put the resource type choice directly before the topic field. Keep provider, limit, timeout, query expansion, and ranking settings in a collapsed-by-default advanced section.
- The selected result is highlighted in the list and its metadata, provenance, links, and any ranking signals appear in the detail pane.
- Keep saved-search selection and Load visible, and group refresh, database selection, export, and download in a compact Actions menu. Export requires a loaded search; downloading requires an explicit file selection.
- On first run, explain that users can start with a topic and that results will appear for review; avoid empty-state instructions that assume a result is already selected.
- Run network and model work outside the GUI thread. Keep status and errors visible, and do not claim cancellation unless the operation can actually be stopped.
