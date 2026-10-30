"""Responsive PySide6 desktop interface with background work tasks."""

from __future__ import annotations

from html import escape
import sys
from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from app.core.topic import MAX_TOPIC_LENGTH, validate_topic
from app.gui.controller import ResearchController, SearchResult
from app.providers.openalex import MAX_RESULTS


def safe_http_url(value: str) -> bool:
    """Allow only ordinary HTTP(S) URLs when opening provider-supplied links."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None)


class _WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class _Worker(QRunnable):
    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            value = self.operation()
        except Exception as exc:
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.succeeded.emit(value)
        finally:
            self.signals.finished.emit()


class ResearchWindow(QMainWindow):
    """Desktop search and review surface built directly in Qt widgets."""

    def __init__(self, db_path: str | None = None, *, controller: ResearchController | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Research Discovery")
        self.resize(1180, 780)
        self.setMinimumSize(600, 520)
        self.controller = controller or ResearchController(db_path)
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[_Worker] = set()
        self.current_result: SearchResult | None = None
        self._splitter: QSplitter | None = None
        self._build_ui()
        self._render_empty()

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 14)
        layout.setSpacing(10)

        heading = QLabel("Research Discovery")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Search papers and datasets. Review sources, metadata, and saved results.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(heading)
        layout.addWidget(subtitle)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Search for"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("Papers", "papers")
        self.type_combo.addItem("Datasets", "datasets")
        self.type_combo.setMinimumWidth(170)
        type_row.addWidget(self.type_combo)
        type_row.addStretch(1)
        layout.addLayout(type_row)

        topic_row = QHBoxLayout()
        self.topic_edit = QLineEdit()
        self.topic_edit.setPlaceholderText("Research topic, e.g. urban heat islands")
        self.topic_edit.setMaxLength(MAX_TOPIC_LENGTH)
        self.topic_edit.returnPressed.connect(self._search)
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("primaryButton")
        self.search_button.clicked.connect(self._search)
        topic_row.addWidget(self.topic_edit, 1)
        topic_row.addWidget(self.search_button)
        layout.addLayout(topic_row)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Advanced search settings  +")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.advanced_toggle.setObjectName("advancedToggle")
        layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced_panel = QWidget()
        controls_panel = QVBoxLayout(self.advanced_panel)
        controls_panel.setContentsMargins(0, 0, 0, 0)
        controls_panel.setSpacing(7)
        controls = QGridLayout()
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(5)
        self.provider_combo = QComboBox()
        for label, value in (("OpenAlex", "openalex"), ("Crossref", "crossref"),
                             ("OpenAlex + Crossref", "all")):
            self.provider_combo.addItem(label, value)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, MAX_RESULTS)
        self.limit_spin.setValue(10)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 120.0)
        self.timeout_spin.setValue(10.0)
        self.timeout_spin.setSingleStep(1.0)
        self.timeout_spin.setSuffix(" s")
        self.expand_check = QCheckBox("Expand query")
        self.expand_limit = QSpinBox()
        self.expand_limit.setRange(0, 4)
        self.expand_limit.setValue(4)
        self.expand_limit.setPrefix("Variations ")
        self.expand_limit.setEnabled(False)
        self.expand_check.toggled.connect(self.expand_limit.setEnabled)
        for col, (label, widget) in enumerate((("Paper provider", self.provider_combo),
                                               ("Result limit", self.limit_spin),
                                               ("Timeout", self.timeout_spin))):
            controls.addWidget(QLabel(label), 0, col)
            controls.addWidget(widget, 1, col)
        controls.addWidget(self.expand_check, 1, 3)
        controls.addWidget(self.expand_limit, 1, 4)
        self.source_note = QLabel("Paper sources: OpenAlex, Crossref, or both")
        self.source_note.setObjectName("muted")
        controls.addWidget(self.source_note, 0, 3, 1, 2)
        self.type_combo.currentIndexChanged.connect(self._sync_provider_control)
        controls_panel.addLayout(controls)

        ranking_row = QHBoxLayout()
        self.ranking_combo = QComboBox()
        for label, value in (("Provider order", "none"), ("Semantic similarity", "semantic"),
                             ("Hybrid score", "hybrid")):
            self.ranking_combo.addItem(label, value)
        self.model_edit = QLineEdit("sentence-transformers/all-MiniLM-L6-v2")
        self.model_edit.setMinimumWidth(180)
        hint = QLabel("Optional model scoring runs in the background; first use may fetch model files.")
        hint.setObjectName("muted")
        ranking_row.addWidget(QLabel("Ranking"))
        ranking_row.addWidget(self.ranking_combo)
        ranking_row.addWidget(QLabel("Model"))
        ranking_row.addWidget(self.model_edit, 1)
        ranking_row.addWidget(hint, 2)
        controls_panel.addLayout(ranking_row)
        self.advanced_panel.setVisible(False)
        layout.addWidget(self.advanced_panel)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

        history_row = QHBoxLayout()
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(140)
        self.history_combo.setPlaceholderText("No saved searches yet")
        self.database_button = QPushButton("Choose database…", self)
        self.database_button.clicked.connect(self._choose_database)
        self.refresh_history_button = QPushButton("Refresh history", self)
        self.refresh_history_button.clicked.connect(self._refresh_history)
        self.load_button = QPushButton("Load", self)
        self.load_button.clicked.connect(self._load_selected)
        self.export_csv_button = QPushButton("Export CSV", self)
        self.export_csv_button.clicked.connect(lambda: self._export("csv"))
        self.export_json_button = QPushButton("Export JSON", self)
        self.export_json_button.clicked.connect(lambda: self._export("json"))
        self.download_button = QPushButton("Download selected file…", self)
        self.download_button.clicked.connect(self._download)
        for helper_button in (self.database_button, self.refresh_history_button,
                              self.export_csv_button, self.export_json_button,
                              self.download_button):
            helper_button.hide()
        history_row.addWidget(QLabel("Saved searches"))
        history_row.addWidget(self.history_combo, 1)
        history_row.addWidget(self.load_button)
        self.actions_button = QToolButton()
        self.actions_button.setText("Actions  ▾")
        self.actions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.actions_menu = QMenu(self.actions_button)
        self.action_items = {}
        for label, key, button in (
                ("Refresh history", "refresh", self.refresh_history_button),
                ("Export CSV…", "csv", self.export_csv_button),
                ("Export JSON…", "json", self.export_json_button),
                ("Download selected file…", "download", self.download_button),
                ("Choose database…", "database", self.database_button)):
            action = self.actions_menu.addAction(label)
            action.triggered.connect(button.click)
            self.action_items[key] = action
        self.actions_button.setMenu(self.actions_menu)
        history_row.addWidget(self.actions_button)
        layout.addLayout(history_row)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_table = QTableWidget(0, 6)
        self.results_table.setHorizontalHeaderLabels(("#", "Title", "Type", "Year", "Source", "Score"))
        self.results_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setShowGrid(True)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setMinimumWidth(280)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.results_table.currentCellChanged.connect(self._show_current_resource)
        self.detail_view = QTextBrowser()
        self.detail_view.setOpenLinks(False)
        self.detail_view.document().setDefaultStyleSheet("""
            body { background: #111713; color: #E5ECE6; font-family: 'Cascadia Mono', 'Consolas', monospace; }
            h2 { color: #C6FF4A; font-size: 13pt; font-weight: 700; margin: 0 0 12px 0; }
            h3 { color: #C6FF4A; font-size: 10pt; border-bottom: 1px solid #303A32; padding-bottom: 4px; margin-top: 18px; }
            p, li { color: #D2DBD4; line-height: 1.35; }
            b { color: #F2F6F2; }
            a { color: #C6FF4A; text-decoration: underline; }
        """)
        self.detail_view.anchorClicked.connect(self._open_external_url)
        self._splitter.addWidget(self.results_table)
        self._splitter.addWidget(self.detail_view)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        layout.addWidget(self._splitter, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setCentralWidget(root)
        self._sync_provider_control()
        self._update_splitter_orientation()
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0B0C0A; }
            QWidget { color: #E5ECE6; font-family: "Cascadia Mono", "Consolas", monospace; font-size: 9pt; }
            QLabel#pageTitle { font-size: 18pt; font-weight: 700; color: #C6FF4A; }
            QLabel#subtitle { color: #AAB6AC; }
            QLabel#muted { color: #849187; font-size: 8pt; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget, QTextBrowser {
                background: #111713; color: #E5ECE6; border: 1px solid #303A32; padding: 5px;
                selection-background-color: #34451F; selection-color: #F2FFD9;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #C6FF4A; }
            QComboBox QAbstractItemView { background: #111713; color: #E5ECE6; border: 1px solid #303A32; selection-background-color: #34451F; }
            QTableWidget { gridline-color: #29332B; alternate-background-color: #111713; }
            QHeaderView::section { background: #111713; color: #AAB6AC; border: 0; border-bottom: 1px solid #303A32; padding: 6px; font-weight: 600; }
            QPushButton { background: #111713; color: #D5DED7; border: 1px solid #384239; padding: 6px 9px; }
            QPushButton:hover { border-color: #C6FF4A; color: #F2FFD9; }
            QPushButton:disabled { color: #647067; border-color: #252D27; }
            QPushButton#primaryButton { background: #C6FF4A; color: #111713; font-weight: 700; border: 1px solid #C6FF4A; padding: 8px 16px; }
            QPushButton#primaryButton:hover { background: #D7FF78; border-color: #D7FF78; }
            QToolButton { background: #111713; color: #D5DED7; border: 1px solid #384239; padding: 6px 9px; }
            QToolButton:hover { border-color: #C6FF4A; color: #F2FFD9; }
            QToolButton#advancedToggle { background: transparent; border: 0; color: #AAB6AC; padding: 2px 0; }
            QMenu { background: #111713; color: #E5ECE6; border: 1px solid #384239; }
            QMenu::item { padding: 6px 24px 6px 10px; }
            QMenu::item:selected { background: #34451F; color: #F2FFD9; }
            QCheckBox { color: #D5DED7; spacing: 6px; }
            QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #59665C; background: #111713; }
            QCheckBox::indicator:checked { background: #C6FF4A; border-color: #C6FF4A; }
            QSplitter::handle { background: #303A32; }
            QScrollBar:vertical { background: #0B0C0A; width: 10px; margin: 0; }
            QScrollBar::handle:vertical { background: #384239; min-height: 24px; }
            QScrollBar:horizontal { background: #0B0C0A; height: 10px; margin: 0; }
            QScrollBar::handle:horizontal { background: #384239; min-width: 24px; }
            QLabel#status { color: #B9C5BC; padding: 3px 2px; }
            QToolTip { background: #111713; color: #E5ECE6; border: 1px solid #384239; }
        """)

    def _sync_provider_control(self, *_: Any) -> None:
        datasets = self.type_combo.currentData() == "datasets"
        self.provider_combo.setEnabled(not datasets)
        self.source_note.setText("Dataset source: Harvard Dataverse" if datasets
                                  else "Paper sources: OpenAlex, Crossref, or both")

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setText("Advanced search settings  −" if expanded
                                     else "Advanced search settings  +")

    def _search(self) -> None:
        try:
            topic = validate_topic(self.topic_edit.text())
        except ValueError as exc:
            self.status_label.setText(str(exc))
            QMessageBox.warning(self, "Check the topic", str(exc))
            return
        dataset_mode = self.type_combo.currentData() == "datasets"
        options = {
            "topic": topic,
            "resource_type": self.type_combo.currentData(),
            "provider": "openalex" if dataset_mode else self.provider_combo.currentData(),
            "limit": self.limit_spin.value(),
            "timeout": self.timeout_spin.value(),
            "expand": self.expand_check.isChecked(),
            "expansion_limit": self.expand_limit.value(),
            "ranking": self.ranking_combo.currentData(),
            "model_id": self.model_edit.text().strip() or None,
        }
        ranking = options["ranking"]
        self._run_async(lambda: self.controller.search(**options), self._accept_search,
                        f"Searching for {topic}…" + (" Model scoring runs after retrieval." if ranking != "none" else ""))

    def _refresh_history(self) -> None:
        self._run_async(self.controller.history, self._accept_history, "Loading saved searches…")

    def _choose_database(self) -> None:
        current = str(self.controller.db_path)
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Choose a saved-search database", current,
            "Research databases (*.db *.sqlite *.sqlite3);;All files (*)",
        )
        if not path:
            return
        self.controller = ResearchController(path)
        self.current_result = None
        self.results_table.setRowCount(0)
        self.history_combo.clear()
        self._render_empty("Database changed. Select a saved search or start a new search.")
        self.status_label.setText(f"Database: {path}")
        self._refresh_history()

    def _accept_history(self, summaries: list[Any]) -> None:
        self.history_combo.clear()
        for summary in summaries:
            partial = " · partial" if (summary.metadata or {}).get("status") == "partial" else ""
            self.history_combo.addItem(
                f"#{summary.id} · {summary.topic} · {summary.result_count} results{partial}", summary.id
            )
        if self.current_result is None:
            self.status_label.setText(f"Loaded {len(summaries)} saved searches")

    def _load_selected(self) -> None:
        search_id = self.history_combo.currentData()
        if search_id is None:
            self.status_label.setText("Choose a saved search first")
            return
        self._run_async(lambda: self.controller.show(int(search_id)), self._accept_search,
                        f"Loading search #{search_id}…")

    def _export(self, file_format: str) -> None:
        search_id = self._current_search_id()
        if search_id is None:
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, f"Export {file_format.upper()}", f"research-search-{search_id}.{file_format}",
            f"{file_format.upper()} files (*.{file_format})",
        )
        if not path:
            return
        self._run_async(lambda: self.controller.export(search_id, path, file_format),
                        lambda output: self.status_label.setText(f"Exported to {output}"),
                        f"Writing {file_format.upper()} export…")

    def _download(self) -> None:
        result = self.current_result
        if result is None:
            self.status_label.setText("Load or run a search first")
            return
        row = self.results_table.currentRow()
        if row < 0 or row >= len(result.resources):
            self.status_label.setText("Select a result first")
            return
        files = result.resources[row].downloadable_files
        if not files:
            self.status_label.setText("This result has no provider-supplied files")
            return
        labels = [f"{index}. {item.filename or 'Unknown filename'} · {item.file_format or 'Unknown format'} · "
                  f"{'restricted' if item.is_open_access is False else ('access unknown' if item.is_open_access is None else 'provider-marked public')}"
                  for index, item in enumerate(files, 1)]
        choice, accepted = QInputDialog.getItem(self, "Choose a file", "Select one file to download:",
                                                labels, 0, False)
        if not accepted:
            return
        file_number = labels.index(choice) + 1
        search_id, resource_number = result.search_id, row + 1
        self._run_async(
            lambda: self.controller.download(search_id, resource_number, file_number),
            lambda output: self.status_label.setText(f"Downloaded selected file to {output}"),
            "Downloading selected file…",
        )

    def _current_search_id(self) -> int | None:
        if self.current_result is None:
            self.status_label.setText("Run or load a search first")
            return None
        return self.current_result.search_id

    def _run_async(self, operation: Callable[[], Any], on_success: Callable[[Any], None],
                   progress: str) -> None:
        worker = _Worker(operation)
        self._workers.add(worker)
        worker.signals.succeeded.connect(on_success)
        worker.signals.failed.connect(self._on_error)
        worker.signals.finished.connect(lambda current=worker: self._worker_finished(current))
        self._set_busy(True)
        self.status_label.setText(progress)
        self.thread_pool.start(worker)

    def _worker_finished(self, worker: _Worker) -> None:
        self._workers.discard(worker)
        self._set_busy(bool(self._workers))

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.search_button, self.refresh_history_button, self.load_button,
                       self.database_button, self.export_csv_button, self.export_json_button,
                       self.download_button):
            widget.setEnabled(not busy)
        for action in self.action_items.values():
            action.setEnabled(not busy)

    def _on_error(self, message: str) -> None:
        self.status_label.setText(f"Operation failed: {message}")
        QMessageBox.critical(self, "Research Discovery", message)

    def _accept_search(self, result: SearchResult) -> None:
        self.current_result = result
        self.results_table.setRowCount(len(result.resources))
        for index, resource in enumerate(result.resources, 1):
            score = result.display_scores.get(self.controller._resource_key(resource), {})
            score_value = score.get("hybrid") if score.get("hybrid") is not None else score.get("semantic")
            score_cell = "—" if score_value is None else f"{score_value:.3f}"
            title = resource.title or "Unknown title"
            if score_value is not None:
                title += f" · score {score_value:.3f}"
            values = (str(index), title, resource.resource_type.value,
                      str(resource.publication_year or "Unknown"), resource.source_provider, score_cell)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.results_table.setItem(index - 1, column, cell)
        self.results_table.resizeRowsToContents()
        status = f"Search #{result.search_id}: {result.topic} · {len(result.resources)} results"
        if result.metadata.get("status") == "partial":
            failures = result.metadata.get("failed_providers", {})
            rendered = "; ".join(f"{name}: {message}" for name, message in failures.items())
            status += " · PARTIAL provider results" + (f" ({rendered})" if rendered else "")
        if result.ranking_error:
            status += f" · Ranking failed: {result.ranking_error}. Saved results are shown."
        self.status_label.setText(status)
        if result.resources:
            self.results_table.setCurrentCell(0, 0)
        else:
            self._render_empty(f"No results for “{result.topic}”")
        self._refresh_history()
        self.status_label.setText(status)

    def _show_current_resource(self, row: int, *_: Any) -> None:
        result = self.current_result
        if result is None or row < 0 or row >= len(result.resources):
            return
        resource = result.resources[row]
        html = [f"<h2>{escape(resource.title or 'Unknown title')}</h2>",
                f"<p><b>Result:</b> {row + 1} &nbsp; <b>Type:</b> {escape(resource.resource_type.value)} &nbsp; "
                f"<b>Provider:</b> {escape(resource.source_provider)}</p>"]
        if resource.resource_type.value == "paper":
            page_link = next((link for kind in ("landing_page", "publisher", "doi", "repository", "open_access")
                              for link in resource.links if link.kind.value == kind), None)
            html.append("<h3>Paper access</h3>")
            if page_link is not None:
                html.append(f"<p><b>Paper page:</b> {self._link_markup(page_link.url, 'Open paper page')} "
                            f"({escape(page_link.source_provider)})</p>")
                if not resource.downloadable_files:
                    html.append("<p>No direct file was supplied by the provider. Open the paper page above "
                                "to view the paper or its access options.</p>")
            else:
                html.append("<p>No paper page link was supplied by the provider.</p>")
        values = (
            ("Year", resource.publication_year), ("DOI", resource.doi),
            ("Authors", ", ".join(resource.authors or ()) if resource.authors is not None else None),
            ("License", resource.license), ("Access status", self._access_label(resource.is_open_access)),
            ("External ID", resource.external_id), ("Retrieved", resource.retrieved_at.isoformat()),
        )
        for label, value in values:
            rendered = "Unknown" if value is None or value == "" else str(value)
            html.append(f"<p><b>{escape(label)}:</b> {escape(rendered)}</p>")
        html.append(f"<h3>Description</h3><p>{escape(resource.description or 'Unknown')}</p>")
        html.append(f"<p><b>Keywords:</b> {escape('; '.join(resource.keywords) if resource.keywords is not None else 'Unknown')}</p>")
        if resource.provenance:
            html.append("<h3>Provider provenance</h3><ul>")
            for item in resource.provenance:
                html.append(f"<li>{escape(item.source_provider)} · ID: {escape(item.external_id or 'Unknown')} · "
                            f"DOI: {escape(item.doi or 'Unknown')} · retrieved {escape(item.retrieved_at.isoformat())}</li>")
            html.append("</ul>")
        score = result.display_scores.get(self.controller._resource_key(resource), {})
        if score:
            html.append("<h3>Ranking signals</h3><ul>")
            for name, value in score.items():
                rendered = "unavailable" if value is None else f"{value:.4f}"
                html.append(f"<li><b>{escape(name.title())}:</b> {rendered}</li>")
            html.append("</ul>")
        html.append("<h3>Source links</h3>")
        if resource.links:
            html.append("<ul>")
            for link in resource.links:
                url_markup = self._link_markup(link.url, link.url)
                html.append(f"<li>{escape(link.kind.value)}: {url_markup} ({escape(link.source_provider)})</li>")
            html.append("</ul>")
        else:
            html.append("<p>No source links supplied.</p>")
        if resource.downloadable_files:
            html.append("<h3>Provider-supplied files</h3><ul>")
            for index, item in enumerate(resource.downloadable_files, 1):
                access = "restricted" if item.is_open_access is False else (
                    "unknown" if item.is_open_access is None else "provider-marked public")
                html.append(f"<li>File {index}: {escape(item.filename or 'Unknown filename')} · "
                            f"{escape(item.file_format or 'Unknown format')} · {escape(access)} · "
                            f"{self._link_markup(item.url, 'Open source URL')}</li>")
            html.append("</ul>")
        if result.ranking_error:
            html.append(f"<h3>Ranking error</h3><p>{escape(result.ranking_error)}. "
                        "Saved search results are preserved.</p>")
        self.detail_view.setHtml("\n".join(html))

    @staticmethod
    def _access_label(value: bool | None) -> str:
        return "unknown" if value is None else ("open access" if value else "restricted")

    @staticmethod
    def _link_markup(url: str, label: str) -> str:
        safe_url = escape(url, quote=True)
        if safe_http_url(url):
            return f"<a href=\"{safe_url}\">{escape(label)}</a>"
        return f"{escape(label)} ({escape(url)}; unsupported URL scheme)"

    def _open_external_url(self, url: QUrl) -> None:
        candidate = url.toString()
        if safe_http_url(candidate):
            QDesktopServices.openUrl(url)
        else:
            self.status_label.setText("Blocked a source link that was not a valid HTTP(S) URL")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_splitter_orientation()

    def _update_splitter_orientation(self) -> None:
        if self._splitter is not None:
            orientation = Qt.Orientation.Vertical if self.width() < 820 else Qt.Orientation.Horizontal
            self._splitter.setOrientation(orientation)

    def _render_empty(self, message: str = "Start with a topic above. Your papers and datasets will appear here; select any result to review its details and source links.") -> None:
        self.detail_view.setHtml(f"<h2>Ready to search</h2><p>{escape(message)}</p>")


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    window = ResearchWindow()
    window.show()
    return app.exec()
