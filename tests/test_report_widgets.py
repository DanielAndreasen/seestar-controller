"""Widget-layer tests for session-report ticket 03 — these SKIP in the
headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_observing_widgets.py.

What is asserted when they run (session-report ticket 03): the
:class:`~seestar_controller.observing.report_panel.SessionReportPanel` — the
per-session report action area embedded in the
:class:`~seestar_controller.observing.session_log_dialog.SessionLogDialog` —

- renders the **Generate report** / **Export PDF** buttons, a status line and
  an initially empty saved-reports list;
- **Generate report** runs :func:`~seestar_controller.report.generate_report`
  on a WORKER thread (the panel is ``busy`` and the buttons insensitive while
  it runs; a second click is ignored) and marshals the result back with
  ``GLib.idle_add`` (the tests pump the default main context): the HTML lands
  in ``<project>/reports/<session_id>.html``, the status line shows the saved
  file, the saved-reports list gains a row, and the report is opened through
  the injected ``open_uri`` recorder (the app binds ``Gtk.show_uri``);
- **Export PDF** does the same with
  :func:`~seestar_controller.report.generate_pdf`: a non-empty valid PDF
  (``%PDF-`` header) next to the HTML, opened and listed;
- activating a saved-report row opens that file;
- a failure (unknown session) lands on the status line with the ``error``
  css class, the buttons become sensitive again, and nothing crashes;
- the session log dialog embeds the panel for its capture's session.

The pure PDF/list logic is covered headless in ``tests/test_report_pdf.py``;
the package boundary + lazy export audit lives in
``tests/test_observing_queue.py`` / ``tests/test_report_boundary.py``.
"""

import threading
import time
from datetime import UTC, datetime

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Gtk

from seestar_controller import observing
from seestar_controller.link.mock_scope import make_fits_bytes
from seestar_controller.observing import SessionLogDialog, SessionReportPanel
from seestar_controller.report import pdf_path, report_path
from seestar_controller.store import (
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    start_session,
    store_frame,
)


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


def _pump_main_context(rounds: int = 40) -> None:
    """Iterate the default main context until idle, so pending idle sources
    (the worker results marshalled via ``GLib.idle_add``) run."""
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    """Pump the main loop until ``predicate`` holds (the report worker is a
    real thread; its result arrives via ``GLib.idle_add``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump_main_context()
        if predicate():
            return True
        time.sleep(0.02)
    _pump_main_context()
    return predicate()


@pytest.fixture()
def project_session(tmp_path):
    """A real (pure) project with one closed session: one sub-frame +
    measurement — enough for a real report."""
    project = create_project("Report Night", root=tmp_path / "projects")
    sid = start_session(project)
    frame = store_frame(
        project,
        sid,
        KIND_SUB,
        make_fits_bytes(when=datetime.now(UTC), exposure=10.0, gain=50),
    )
    add_measurement(project, frame.id, 12.5, jd=2460000.5, mag_err=0.02)
    close_session(project, sid)
    yield project, sid
    project.close()


@pytest.fixture()
def opened():
    """The ``open_uri`` recorder: every opened path lands here."""
    return []


def make_panel(project, sid, opened):
    """A SessionReportPanel over the tmp project with the recorder as
    ``open_uri`` (the app binds ``Gtk.show_uri``) and the real
    ``GLib.idle_add`` marshalling."""
    return SessionReportPanel(project, sid, open_uri=opened.append)


# --- layout + lazy export ------------------------------------------------------------


def test_report_panel_is_a_lazy_widget_export_of_the_observing_package():
    assert observing._WIDGET_EXPORTS["SessionReportPanel"] == "report_panel"
    assert "SessionReportPanel" in observing.__all__
    assert observing.SessionReportPanel is SessionReportPanel


def test_panel_layout_and_initially_empty_reports(gtk, project_session, opened):
    project, sid = project_session
    panel = make_panel(project, sid, opened)
    assert panel.project is project
    assert panel.session_id == sid
    assert panel.generate_button.get_label() == "Generate report"
    assert panel.pdf_button.get_label() == "Export PDF"
    assert panel.status_label.get_label() == ""
    assert not panel.busy
    assert panel.report_row_count() == 0
    assert panel.generate_button.get_sensitive()
    assert panel.pdf_button.get_sensitive()


# --- Generate report: worker thread + GLib.idle_add marshalling ----------------------


def test_generate_button_saves_and_opens_the_html_and_lists_it(gtk, project_session, opened):
    project, sid = project_session
    panel = make_panel(project, sid, opened)
    panel.generate_button.emit("clicked")
    assert panel.busy  # the worker thread is running
    assert not panel.generate_button.get_sensitive()
    assert "generating" in panel.status_label.get_label()
    assert _wait_for(lambda: not panel.busy)
    html = report_path(project, sid)
    assert html.is_file() and html.stat().st_size > 1000
    assert "saved" in panel.status_label.get_label() and html.name in panel.status_label.get_label()
    assert not panel.status_label.has_css_class("error")
    assert opened == [html]  # opened via the injected open_uri (Gtk.show_uri in the app)
    assert panel.report_row_count() == 1
    assert html.name in panel.report_row_label(0)
    assert panel.report_row_path(0) == html
    assert panel.generate_button.get_sensitive()  # buttons re-enabled


def test_pdf_button_produces_a_non_empty_pdf_next_to_the_html(gtk, project_session, opened):
    project, sid = project_session
    panel = make_panel(project, sid, opened)
    panel.generate_button.emit("clicked")
    assert _wait_for(lambda: not panel.busy)
    panel.pdf_button.emit("clicked")
    assert panel.busy
    assert _wait_for(lambda: not panel.busy)
    html = report_path(project, sid)
    pdf = pdf_path(project, sid)
    assert pdf.parent == html.parent  # next to the HTML
    raw = pdf.read_bytes()
    assert raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF") and len(raw) > 1000
    assert opened == [html, pdf]
    assert panel.report_row_count() == 2
    labels = [panel.report_row_label(i) for i in range(2)]
    assert any(html.name in label for label in labels)
    assert any(pdf.name in label for label in labels)
    assert {panel.report_row_path(i) for i in range(2)} == {html, pdf}


def test_saved_report_row_activation_opens_the_file(gtk, project_session, opened):
    project, sid = project_session
    panel = make_panel(project, sid, opened)
    panel.generate_button.emit("clicked")
    assert _wait_for(lambda: not panel.busy)
    assert panel.report_row_count() == 1
    row = panel.reports_listbox.get_row_at_index(0)
    assert row is not None
    panel.reports_listbox.emit("row-activated", row)
    assert opened == [report_path(project, sid), report_path(project, sid)]


def test_one_worker_at_a_time_while_busy(gtk, project_session, opened, monkeypatch):
    """While a report generation runs, the buttons are insensitive and a
    second click does not start another worker (the busy guard)."""
    import seestar_controller.observing.report_panel as report_panel_module

    project, sid = project_session
    panel = make_panel(project, sid, opened)
    release = threading.Event()
    calls: list[str] = []

    def fake_generate(project_arg, session_id):
        calls.append(session_id)
        release.wait(timeout=10)
        return report_path(project_arg, session_id)

    monkeypatch.setattr(report_panel_module, "generate_report", fake_generate)
    panel.generate_button.emit("clicked")
    assert panel.busy
    assert not panel.generate_button.get_sensitive()
    assert not panel.pdf_button.get_sensitive()
    panel.pdf_button.emit("clicked")  # ignored while busy
    panel.generate_button.emit("clicked")  # ignored while busy
    release.set()
    assert _wait_for(lambda: not panel.busy)
    assert calls == [sid]  # exactly one worker ran
    assert panel.generate_button.get_sensitive()


def test_unknown_session_shows_an_inline_error_and_recovers(gtk, tmp_path, opened):
    project = create_project("Empty Night", root=tmp_path / "projects")
    try:
        panel = make_panel(project, "no-such-session", opened)
        panel.generate_button.emit("clicked")
        assert _wait_for(lambda: not panel.busy)
        label = panel.status_label.get_label()
        assert "ReportError" in label
        assert panel.status_label.has_css_class("error")
        assert opened == []  # nothing was opened
        assert panel.report_row_count() == 0
        assert panel.generate_button.get_sensitive()  # recovered
        # the PDF button fails the same way (no crash)
        panel.pdf_button.emit("clicked")
        assert _wait_for(lambda: not panel.busy)
        assert "ReportError" in panel.status_label.get_label()
    finally:
        project.close()


# --- the session log dialog embeds the panel ------------------------------------------


def test_session_log_dialog_embeds_the_report_panel_for_its_session(gtk, tmp_path):
    from seestar_controller.observing import SessionCapture

    project = create_project("Log Night", root=tmp_path / "projects")
    try:
        sid = start_session(project)
        capture = SessionCapture(object(), project, sid)
        dialog = SessionLogDialog(capture)
        panel = dialog.report_panel
        assert isinstance(panel, SessionReportPanel)
        assert panel.project is project
        assert panel.session_id == sid
        assert panel.generate_button.get_label() == "Generate report"
        assert panel.pdf_button.get_label() == "Export PDF"
        dialog.close()
    finally:
        project.close()
