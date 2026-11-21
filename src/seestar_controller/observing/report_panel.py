"""Session report panel — the WIDGET layer of session-report ticket 03.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure/executor layer or
eagerly by the package ``__init__`` — the package stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`SessionReportPanel` is the per-session report action area embedded
in the :class:`~seestar_controller.observing.session_log_dialog.SessionLogDialog`
(ticket 03): a **Generate report** button and an **Export PDF** button over a
status line and a "saved reports" list.

- **Generate report** runs :func:`~seestar_controller.report.generate_report`
  on a WORKER thread (the report renders matplotlib plots — it must never
  block the main loop, the same pattern as the periodogram worker in
  :mod:`seestar_controller.analysis.period_view`); the worker does NOT use the
  panel's ``project`` handle directly — its sqlite connection was created on
  the main thread (``check_same_thread`` default) and may not be used from
  another thread, so the worker RE-OPENS the project on its own thread
  (:func:`~seestar_controller.store.open_project`; the db runs in WAL mode,
  so a second connection is a safe concurrent reader) and runs the whole
  report (data collection + rendering + atomic write) against that fresh
  connection — every sqlite object stays on the thread that created it. The
  result is marshalled back onto the GLib main loop with ``GLib.idle_add``
  (injectable ``invoke_on_main`` for tests), the report is saved into the
  project (``reports/<session_id>.html``), the saved-reports list refreshes,
  and the HTML is opened with ``Gtk.show_uri`` (injectable ``open_uri`` for
  tests);
- **Export PDF** does the same with
  :func:`~seestar_controller.report.generate_pdf`, producing the PDF next to
  the HTML (``reports/<session_id>.pdf``) and opening it;
- the **saved reports** list shows every ``reports/<session_id>.*`` file
  (:func:`~seestar_controller.report.list_session_reports`); activating a row
  opens that file;
- failures (unknown session, store error, write error) land on the status
  line with the ``error`` css class — never a crash; while a report is being
  generated the buttons are insensitive (one worker at a time).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from seestar_controller.report import (
    generate_pdf,
    generate_report,
    list_session_reports,
)
from seestar_controller.store.project import open_project

logger = logging.getLogger(__name__)

#: Type of the injectable "open a file with the system handler" callable.
OpenUri = Callable[[Path], None]

#: Type of the injectable main-loop marshalling callable (the app binds
#: ``GLib.idle_add``; tests may bind a direct call).
InvokeOnMain = Callable[..., Any]


def _show_uri(path: Path) -> None:
    """The default ``open_uri``: open ``path`` with the system handler via
    ``Gtk.show_uri`` (timestamp 0 = "now")."""
    Gtk.show_uri(None, path.resolve().as_uri(), 0)


class SessionReportPanel(Gtk.Box):
    """The per-session report actions: Generate report / Export PDF buttons,
    a status line, and the list of saved reports of the session.

    - ``project`` — the open store project the reports are saved into;
    - ``session_id`` — the session the report is generated for;
    - ``open_uri`` — how a generated/saved report is opened (default:
      :func:`Gtk.show_uri` via :func:`_show_uri`; tests inject a recorder);
    - ``invoke_on_main`` — the main-loop marshalling callable for the worker
      result (default: ``GLib.idle_add``).
    """

    def __init__(
        self,
        project: Any,
        session_id: str,
        *,
        open_uri: OpenUri | None = None,
        invoke_on_main: InvokeOnMain | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 8)
        super().__init__(**kwargs)
        self._project = project
        self._session_id = session_id
        self._open_uri: OpenUri = open_uri if open_uri is not None else _show_uri
        self._invoke_on_main: InvokeOnMain = (
            invoke_on_main if invoke_on_main is not None else GLib.idle_add
        )
        self._busy = False
        self._worker: threading.Thread | None = None
        self._report_rows: list[Gtk.ListBoxRow] = []
        self._report_paths: list[Path] = []

        group = Adw.PreferencesGroup(title="Session report")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._generate_button = Gtk.Button(
            label="Generate report",
            tooltip_text="Generate the HTML report of this session into the project and open it",
        )
        self._generate_button.connect("clicked", self._on_generate_clicked)
        self._pdf_button = Gtk.Button(
            label="Export PDF",
            tooltip_text="Export the same report as a PDF next to the HTML and open it",
        )
        self._pdf_button.connect("clicked", self._on_pdf_clicked)
        buttons.append(self._generate_button)
        buttons.append(self._pdf_button)
        group.add(buttons)
        self._status_label = Gtk.Label(
            label="", xalign=0.0, wrap=True, css_classes=["dim-label", "caption"]
        )
        group.add(self._status_label)
        self._reports_listbox = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self._reports_listbox.connect("row-activated", self._on_report_row_activated)
        group.add(self._reports_listbox)
        self.append(group)

        self.refresh_reports()

    # -- public helpers (also used by the widget tests) ------------------------

    @property
    def project(self) -> Any:
        """The project the reports are saved into."""
        return self._project

    @property
    def session_id(self) -> str:
        """The session the report is generated for."""
        return self._session_id

    @property
    def generate_button(self) -> Gtk.Button:
        """The 'Generate report' button."""
        return self._generate_button

    @property
    def pdf_button(self) -> Gtk.Button:
        """The 'Export PDF' button."""
        return self._pdf_button

    @property
    def status_label(self) -> Gtk.Label:
        """The status line (progress / saved path / error)."""
        return self._status_label

    @property
    def reports_listbox(self) -> Gtk.ListBox:
        """The saved-reports list."""
        return self._reports_listbox

    @property
    def busy(self) -> bool:
        """True while a report generation runs on the worker thread."""
        return self._busy

    def report_row_count(self) -> int:
        """The number of listed saved reports."""
        return len(self._report_rows)

    def report_row_label(self, index: int) -> str:
        """The label of saved-report row ``index``."""
        row = self._report_rows[index]
        label = row.get_child()
        return label.get_label() if label is not None else ""

    def report_row_path(self, index: int) -> Path:
        """The file path of saved-report row ``index``."""
        return self._report_paths[index]

    def refresh_reports(self) -> None:
        """Rebuild the saved-reports list from the project's ``reports/``
        directory (best-effort — a store problem must not break the panel)."""
        for row in self._report_rows:
            self._reports_listbox.remove(row)
        self._report_rows = []
        self._report_paths = []
        try:
            reports = list_session_reports(self._project, self._session_id)
        except Exception:
            logger.debug("could not list the saved reports", exc_info=True)
            reports = ()
        for path in reports:
            label = Gtk.Label(
                label=f"{path.name}  ({path.stat().st_size} bytes)",
                xalign=0.0,
                margin_top=6,
                margin_bottom=6,
                margin_start=12,
                margin_end=12,
            )
            row = Gtk.ListBoxRow(child=label, activatable=True)
            self._report_rows.append(row)
            self._report_paths.append(path)
            self._reports_listbox.append(row)

    # -- button handlers ---------------------------------------------------------

    def _on_generate_clicked(self, *_args) -> None:
        """Generate report: run ``generate_report`` on a worker thread."""
        self._run_worker("HTML report", generate_report)

    def _on_pdf_clicked(self, *_args) -> None:
        """Export PDF: run ``generate_pdf`` on a worker thread."""
        self._run_worker("PDF report", generate_pdf)

    def _run_worker(self, what: str, func: Callable[[Any, str], Path]) -> None:
        """Run ``func(project, session_id)`` on a daemon worker thread (the
        report rendering must never block the main loop); the outcome is
        marshalled back with ``invoke_on_main`` (``GLib.idle_add`` in the app).
        One worker at a time — a second click while busy is ignored.

        The worker runs ``func`` against a FRESH store connection opened on
        its own thread (:meth:`_run_with_worker_store`) — never against
        ``self._project``, whose db connection is main-thread-bound."""
        if self._busy:
            return
        self._busy = True
        self._set_buttons_sensitive(False)
        self._set_status(f"generating {what}…", error=False)

        def work() -> None:
            try:
                path = self._run_with_worker_store(func)
            except Exception as exc:  # noqa: BLE001 - surfaced on the status line
                outcome: tuple[bool, Path | None, Exception | None] = (False, None, exc)
            else:
                outcome = (True, path, None)
            self._invoke_on_main(self._on_worker_done, what, outcome)

        self._worker = threading.Thread(
            target=work, name="session-report-worker", daemon=True
        )
        self._worker.start()

    def _run_with_worker_store(self, func: Callable[[Any, str], Path]) -> Path:
        """Run ``func(project, session_id)`` on the CALLING (worker) thread
        against a fresh store connection opened on that thread, and return
        its result.

        ``self._project`` was opened on the main thread and its sqlite
        connection is bound to that thread (``sqlite3.connect`` default
        ``check_same_thread=True``) — a store read from the worker through
        it raises ``sqlite3.ProgrammingError``. So the project directory is
        re-opened here with :func:`~seestar_controller.store.open_project`:
        the db runs in WAL mode, so a second connection on another thread is
        a safe concurrent reader, and the whole report (data collection,
        plot rendering, atomic file write) runs on the worker against it —
        every sqlite object is used only on the thread that created it and
        the main loop stays free. The fresh connection is closed when done.

        A project handle without a ``path`` (not a real store project) is
        passed through unchanged — ``func`` applies its own contract.
        """
        project_path = getattr(self._project, "path", None)
        if project_path is None:
            return func(self._project, self._session_id)
        with open_project(project_path) as worker_project:
            return func(worker_project, self._session_id)

    def _on_worker_done(self, what: str, outcome: tuple[bool, Path | None, Exception | None]) -> bool:
        """The worker finished (runs on the main loop via ``invoke_on_main``):
        show the result, refresh the saved-reports list, open the report.
        Returns False so a ``GLib.idle_add`` source is removed."""
        ok, path, exc = outcome
        self._busy = False
        self._set_buttons_sensitive(True)
        if ok and path is not None:
            self._set_status(f"saved {path.name}", error=False)
            self.refresh_reports()
            try:
                self._open_uri(path)
            except Exception:
                logger.debug("could not open %s", path, exc_info=True)
                self._set_status(f"saved {path.name} (could not open it)", error=False)
        else:
            assert exc is not None
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
        return False  # GLib.SOURCE_REMOVE

    def _on_report_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """A saved-report row was activated: open that file."""
        try:
            index = self._report_rows.index(row)
        except ValueError:
            return
        try:
            self._open_uri(self._report_paths[index])
        except Exception:
            logger.debug("could not open the saved report", exc_info=True)
            self._set_status("could not open the saved report", error=True)

    # -- internals ---------------------------------------------------------------

    def _set_buttons_sensitive(self, sensitive: bool) -> None:
        self._generate_button.set_sensitive(sensitive)
        self._pdf_button.set_sensitive(sensitive)

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        self._status_label.remove_css_class("error")
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")
