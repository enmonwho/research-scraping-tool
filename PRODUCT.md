# Product brief — Research Discovery

## Product

Research Discovery is a local Python application that helps thesis researchers find and review academic papers and open datasets, preserve trustworthy source links and provenance, and manage saved searches and results.

## Users and jobs

- Primary users: students and researchers doing thesis or literature review work.
- Main job: search a topic across supported academic and dataset sources, inspect real returned records and source evidence, and revisit or export saved results.
- Secondary jobs: select lawful downloadable files and use existing expansion or ranking tools where available.

## Product constraints

- The current milestone adds a native Windows desktop interface in PySide6 and a Windows executable, reusing the Python core.
- The CLI remains supported; n8n is optional and not required for manual research.
- Search may access public provider APIs. Saved data, configuration, downloads, and model cache remain in user-writable local storage.
- Preserve source attribution and actual provider URLs. Do not invent authorship, licensing, open access, relevance, or direct-file links.
- Downloads are user-selected and must use existing safety checks. Network and model operations must report errors truthfully.
- No web app, hosted backend, or mandatory paid service is in scope.

## Platform

Windows desktop UI using native Windows interaction conventions; Python CLI remains available.

## Success signals

- A researcher can search papers or datasets, inspect metadata and provenance, save/reopen history, and export results.
- The packaged application stores user data outside its install folder and reports unavailable features or failures clearly.
