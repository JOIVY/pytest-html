# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import

from base64 import b64encode, b64decode
from os.path import isfile
import datetime
import json
import os
import pkg_resources
import sys
import time
import bisect
import hashlib
import warnings
import re

try:
    from ansi2html import Ansi2HTMLConverter, style

    ANSI = True
except ImportError:
    # ansi2html is not installed
    ANSI = False

from py.xml import html, raw

from . import extras
from . import __version__, __pypi_url__

PY3 = sys.version_info[0] == 3

# Python 2.X and 3.X compatibility
if PY3:
    basestring = str
    from html import escape
else:
    from codecs import open
    from cgi import escape


def pytest_addhooks(pluginmanager):
    from . import hooks
    pluginmanager.add_hookspecs(hooks)


def pytest_addoption(parser):
    group = parser.getgroup('terminal reporting')
    group.addoption('--html', action='store', dest='htmlpath',
                    metavar='path', default=None,
                    help='create html report file at given path.')
    group.addoption('--self-contained-html', action='store_true',
                    help='create a self-contained html file containing all '
                         'necessary styles, scripts, and images - this means '
                         'that the report may not render or function where CSP '
                         'restrictions are in place (see '
                         'https://developer.mozilla.org/docs/Web/Security/CSP)')


def pytest_configure(config):
    htmlpath = config.option.htmlpath
    # prevent opening htmlpath on slave nodes (xdist)
    if htmlpath and not hasattr(config, 'slaveinput'):
        config._html = HTMLReport(htmlpath, config)
        config.pluginmanager.register(config._html)


def pytest_unconfigure(config):
    html = getattr(config, '_html', None)
    if html:
        del config._html
        config.pluginmanager.unregister(html)


def data_uri(content, mime_type='text/plain', charset='utf-8'):
    data = b64encode(content.encode(charset)).decode('ascii')
    return 'data:{0};charset={1};base64,{2}'.format(mime_type, charset, data)


class HTMLReport(object):
    def __init__(self, logfile, config):
        logfile = os.path.expanduser(os.path.expandvars(logfile))
        self.logfile = os.path.abspath(logfile)
        self.test_logs = []
        self.results = []
        self.errors = self.failed = 0
        self.passed = self.skipped = 0
        self.xfailed = self.xpassed = 0
        has_rerun = config.pluginmanager.hasplugin('rerunfailures')
        self.rerun = 0 if has_rerun else None
        self.self_contained = config.getoption('self_contained_html')
        self.config = config

    class TestResult:

        def __init__(self, outcome, report, logfile, config):
            self.test_id = report.nodeid
            if getattr(report, 'when', 'call') != 'call':
                self.test_id = '::'.join([report.nodeid, report.when])
            self.time = getattr(report, 'duration', 0.0)
            self.outcome = outcome
            self.additional_html = []
            self.links_html = []
            self.self_contained = config.getoption('self_contained_html')
            self.logfile = logfile
            self.config = config
            self.row_table = self.row_extra = None

            test_index = hasattr(report, 'rerun') and report.rerun + 1 or 0

            for extra_index, extra in enumerate(getattr(report, 'extra', [])):
                self.append_extra_html(extra, extra_index, test_index)

            self.append_log_html(report, self.additional_html)

            cells = [
                html.td(self.outcome, class_='col-result'),
                html.td(self.test_id, class_='col-name'),
                html.td('{0:.2f}'.format(self.time), class_='col-duration'),
                html.td(self.links_html, class_='col-links')]

            self.config.hook.pytest_html_results_table_row(
                report=report, cells=cells)

            self.config.hook.pytest_html_results_table_html(
                report=report, data=self.additional_html)

            if len(cells) > 0:
                self.row_table = html.tr(cells)
                self.row_extra = html.tr(html.td(self.additional_html,
                                                 class_='extra', colspan=len(cells)))

        def __lt__(self, other):
            order = ('Error', 'Failed', 'Rerun', 'XFailed',
                     'XPassed', 'Passed', 'Skipped')
            return order.index(self.outcome) < order.index(other.outcome)

        def create_asset(self, content, extra_index,
                         test_index, file_extension, mode='w'):
            hash_key = ''.join([self.test_id, str(extra_index),
                                str(test_index)]).encode('utf-8')
            hash_generator = hashlib.md5()
            hash_generator.update(hash_key)
            asset_file_name = '{0}.{1}'.format(hash_generator.hexdigest(),
                                               file_extension)
            asset_path = os.path.join(os.path.dirname(self.logfile),
                                      'assets', asset_file_name)
            if not os.path.exists(os.path.dirname(asset_path)):
                os.makedirs(os.path.dirname(asset_path))

            relative_path = '{0}/{1}'.format('assets', asset_file_name)

            kwargs = {'encoding': 'utf-8'} if 'b' not in mode else {}
            with open(asset_path, mode, **kwargs) as f:
                f.write(content)
            return relative_path

        def append_extra_html(self, extra, extra_index, test_index):
            href = None
            if extra.get('format') == extras.FORMAT_IMAGE:
                content = extra.get('content')
                try:
                    is_uri_or_path = (content.startswith(('file', 'http')) or
                                      isfile(content))
                except ValueError:
                    # On Windows, os.path.isfile throws this exception when
                    # passed a b64 encoded image.
                    is_uri_or_path = False
                if is_uri_or_path:
                    if self.self_contained:
                        warnings.warn('Self-contained HTML report '
                                      'includes link to external '
                                      'resource: {}'.format(content))
                    html_div = html.a(html.img(src=content), href=content)
                elif self.self_contained:
                    src = 'data:{0};base64,{1}'.format(
                        extra.get('mime_type'),
                        content)
                    html_div = html.img(src=src)
                else:
                    if PY3:
                        content = b64decode(content.encode('utf-8'))
                    else:
                        content = b64decode(content)
                    href = src = self.create_asset(
                        content, extra_index, test_index,
                        extra.get('extension'), 'wb')
                    html_div = html.a(html.img(src=src), href=href)
                self.additional_html.append(html.div(html_div, class_='image'))

            elif extra.get('format') == extras.FORMAT_HTML:
                self.additional_html.append(html.div(
                    raw(extra.get('content'))))

            elif extra.get('format') == extras.FORMAT_JSON:
                content = json.dumps(extra.get('content'))
                if self.self_contained:
                    href = data_uri(content,
                                    mime_type=extra.get('mime_type'))
                else:
                    href = self.create_asset(content, extra_index,
                                             test_index,
                                             extra.get('extension'))

            elif extra.get('format') == extras.FORMAT_TEXT:
                content = extra.get('content')
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                if self.self_contained:
                    href = data_uri(content)
                else:
                    href = self.create_asset(content, extra_index,
                                             test_index,
                                             extra.get('extension'))

            elif extra.get('format') == extras.FORMAT_URL:
                href = extra.get('content')

            if href is not None:
                self.links_html.append(html.a(
                    extra.get('name'),
                    class_=extra.get('format'),
                    href=href,
                    target='_blank'))
                self.links_html.append(' ')

        def _append_exception_section(self, div, exc):
            for line in exc.splitlines():
                separator = line.startswith('_ ' * 10)
                if separator:
                    div.append(line[:80])
                    div.append(html.br())
                else:
                    exception = line.startswith("E   ")
                    if exception:
                        div.append(html.span(raw(escape(line)), class_='error'))
                        div.append(html.br())
                    else:
                        div.append(raw(escape(line)))
                div.append(html.br())

        def _append_run_error(self, div, run_error):
            run_error_div = html.div(class_="run_error")

            self._append_exception_section(run_error_div, run_error)

            div.append(
                run_error_div
            )
            div.append(html.br())

        def _get_and_format_test_steps(self, test_steps):
            """
            Go through the list of steps (each step is pretty much a very long string).
            Every step starts with <beginning_of_test_step> and ends with <end_of_test_step>
            so filter out only these items.
            """
            test_steps_formatted = []
            regex = re.compile("<beginning_of_test_step>(.*)<end_of_test_step>", flags=re.S)

            for test_step in test_steps:
                re_search = regex.search(test_step)
                if re_search:
                    test_steps_formatted.append(re_search.group(1))
            return test_steps_formatted

        def _create_test_steps_table(self):
            # Create test steps table
            test_steps_table = html.table(class_="steps_table")

            test_steps_table.append(
                html.th(
                    "Action name",
                    class_="step_name"
                )
            )
            test_steps_table.append(
                html.th("User id", class_="step_user_id")
            )
            test_steps_table.append(
                html.th("Status code", class_="step_status_code")
            )
            test_steps_table.append(
                html.th("Request headers", class_="step_request_headers")
            )

            test_steps_tbody = html.tbody(class_="steps_table_tbody")
            test_steps_table.append(test_steps_tbody)

            return test_steps_table, test_steps_tbody

        def _create_run_divs(self, run, run_error, single_run):
            # Create run main div
            run_main_div = html.div(
                class_="run_main",
                count=run
            )
            # Create run label div
            run_label_div = html.div(
                "Run {run} {pass_or_fail}".format(
                    run=run,
                    pass_or_fail="failed" if run_error else "passed"
                ),
                html.span(" (show/hide)", class_="hint"),
                class_="run_label",
                count=run
            )
            # Create run content div
            run_content_div = html.div(
                class_="run_content",
                count=run
            )
            # If more than one run collapse the run divs
            if not single_run:
                run_content_div.attr.__setattr__("style", "display:none")
            # Add success attribute
            if run_error:
                run_label_div.attr.__setattr__("success", "false")
                run_content_div.attr.__setattr__("success", "false")
                run_main_div.attr.__setattr__("success", "false")
                self._append_run_error(run_content_div, run_error=run_error)
            else:
                run_label_div.attr.__setattr__("success", "true")
                run_content_div.attr.__setattr__("success", "true")
                run_main_div.attr.__setattr__("success", "true")

            run_main_div.append(run_label_div)
            run_main_div.append(run_content_div)

            return run_main_div, run_content_div

        def _create_test_step_divs(self, step_name, step_log):
            # Create test step main div
            test_step_main_div = html.div(
                class_="test_step_main"
            )
            # Create test step show/hide div
            test_step_label_div = html.div(
                step_name,
                html.span(" (show/hide)", class_="hint"),
                class_="test_step_label",
                name=step_name
            )
            # Create test step content div
            test_step_content_div = html.div(
                raw(step_log),
                class_="test_step_content",
                name=step_name,
                style="display:none"
            )

            test_step_main_div.append(test_step_label_div)
            test_step_main_div.append(test_step_content_div)

            return test_step_main_div

        def _create_request_headers_divs(self, request_headers):
            # Create request headers main div
            request_headers_main_div = html.div(
                class_="request_headers_main"
            )
            # Create request headers show/hide div
            request_headers_label_div = html.div(
                html.span("show/hide headers", class_="hint"),
                class_="request_headers_label"
            )
            # Create request headers content div
            request_headers_content_div = html.div(
                raw(request_headers),
                class_="request_headers_content",
                style="display:none"
            )

            request_headers_main_div.append(request_headers_label_div)
            request_headers_main_div.append(request_headers_content_div)

            return request_headers_main_div

        def _create_test_steps_tr(self, test_step_main_div, user_id,
                                  status_code, request_headers_main_div):
            test_steps_tr = html.tr(class_="steps_table_tr")

            test_steps_tr.append(
                html.td(test_step_main_div, class_="tr_step_name")
            )
            test_steps_tr.append(
                html.td(raw(user_id), class_="tr_step_user_id")
            )
            test_steps_tr.append(
                html.td(raw(status_code), class_="tr_step_status_code")
            )
            test_steps_tr.append(
                html.td(request_headers_main_div, class_="tr_step_request_headers")
            )

            return test_steps_tr

        def _create_test_debug_logs_divs(self, test_debug_logs):
            # Create test debug logs main div
            test_debug_logs_main_div = html.div(
                class_="test_debug_logs_main"
            )
            # Create test debug logs show/hide div
            test_debug_logs_label_div = html.div(
                "Test debug logs",
                html.span(" (show/hide)", class_="hint"),
                class_="test_debug_logs_label"
            )
            # Create test debug logs content div
            test_debug_logs_content_div = html.div(
                test_debug_logs,
                class_="test_debug_logs_content",
                style="display:none"
            )

            test_debug_logs_main_div.append(test_debug_logs_label_div)
            test_debug_logs_main_div.append(test_debug_logs_content_div)

            return test_debug_logs_main_div

        def _create_be_stack_trace_divs(self, be_stack_trace):
            # Create be stack trace main div
            be_stack_trace_main_div = html.div(
                class_="be_stack_trace_main"
            )
            # Create be stack trace show/hide div
            be_stack_trace_label_div = html.div(
                "Backend stack trace",
                html.span(" (show/hide)", class_="hint"),
                class_="be_stack_trace_label"
            )
            # Create be stack trace content div
            be_stack_trace_content_div = html.div(
                raw(be_stack_trace),
                class_="be_stack_trace_content",
                style="display:none"
            )

            be_stack_trace_main_div.append(be_stack_trace_label_div)
            be_stack_trace_main_div.append(be_stack_trace_content_div)

            return be_stack_trace_main_div

        def _get_and_format_step_logs(self, test_step):
            re_search = re.search(
                "<beginning_of_step_name>(.*)<end_of_step_name>"
                "<beginning_of_user_id>(.*)<end_of_user_id>"
                "<beginning_of_status_code>(.*)<end_of_status_code>"
                "<beginning_of_request_headers>(.*)<end_of_request_headers>",
                test_step,
                flags=re.S
            )

            _, step_log = test_step.split("<step_log>")

            step_name = re_search.group(1)
            user_id = re_search.group(2)
            status_code = re_search.group(3)
            request_headers = re_search.group(4)

            return step_name, user_id, status_code, request_headers, step_log

        def _get_and_format_test_debug_logs(self, test_logs):
            all_matches = re.findall(
                "<beginning_of_test_debug>(.*?)<end_of_test_debug>",
                test_logs,
                flags=re.S
            )
            if all_matches:
                return "\n\n".join(all_matches)

        def _get_and_format_be_stack_trace(self, test_logs):
            re_search = re.search(
                "<beginning_of_be_stack_trace>(.*)<end_of_be_stack_trace>",
                test_logs,
                flags=re.S
            )
            if re_search:
                be_stack_trace = re_search.group(1)
                return be_stack_trace

        def append_log_html(self, report, additional_html):
            """
            This method has been modified from the original
            and works only with pytest-rerunfailures plugin (which itself has also been modified)
            """
            log = html.div(class_='log')

            # If report is skipped only append the longreprtext to log
            if report.skipped:
                self._append_exception_section(log, report.longreprtext)
            # Otherwise if there are runs_logs (meaning tests have ran) create more logs
            elif hasattr(report, "runs_logs"):  # Only do this if there was at least 1 run
                for run, logs in report.runs_logs.items():
                    # Get run error
                    run_error = report.runs_errors[run]
                    # Create run main, label and content divs
                    run_main_div, run_content_div = self._create_run_divs(
                        run=run,
                        run_error=run_error,
                        single_run=True if len(report.runs_logs) == 1 else False
                    )
                    # Create test steps table
                    test_steps_table, test_steps_tbody = self._create_test_steps_table()

                    for section in logs:
                        header, content = map(escape, section)
                        # run_div.append(' {0} '.format(header).center(80, '-'))
                        # run_div.append(html.br())
                        if "stderr" in header:
                            continue

                        # Get backend stack trace and add to ttest steps tbody if
                        # backend stack trace is present
                        be_stack_trace = self._get_and_format_be_stack_trace(
                            test_logs=section[1]
                        )
                        if be_stack_trace:
                            be_stack_trace_main_div = self._create_be_stack_trace_divs(
                                be_stack_trace=be_stack_trace
                            )
                            run_content_div.append(be_stack_trace_main_div)
                        # Get test debug logs and add to test steps tbody if
                        # any test debug logs are present
                        test_debug_logs = self._get_and_format_test_debug_logs(
                            test_logs=section[1]
                        )
                        if test_debug_logs:
                            test_debug_logs_main_div = self._create_test_debug_logs_divs(
                                test_debug_logs=test_debug_logs
                            )
                            run_content_div.append(test_debug_logs_main_div)

                        # Separate test steps and format test steps
                        # (remove random /n, /s etc. chars)
                        test_steps_unformatted = section[1].split("<split_marker>")
                        test_steps = self._get_and_format_test_steps(test_steps_unformatted)

                        for test_step in test_steps:
                            if ANSI:
                                converter = Ansi2HTMLConverter(inline=False, escaped=False)
                                test_step = converter.convert(test_step, full=False)
                            step_name, user_id, status_code, request_headers, step_log = (
                                self._get_and_format_step_logs(test_step)
                            )

                            # Create test step main div (containing test log)
                            test_step_main_div = self._create_test_step_divs(
                                step_name=step_name,
                                step_log=step_log
                            )

                            # Create request headers main div
                            request_headers_main_div = self._create_request_headers_divs(
                                request_headers=request_headers
                            )

                            # Create test steps table row
                            test_steps_tr = self._create_test_steps_tr(
                                test_step_main_div=test_step_main_div,
                                user_id=user_id,
                                status_code=status_code,
                                request_headers_main_div=request_headers_main_div
                            )

                            # Append table row to table body
                            test_steps_tbody.append(test_steps_tr)

                    # Append test steps table to run div
                    run_content_div.append(test_steps_table)

                    log.append(run_main_div)

            if len(log) == 0:
                log = html.div(class_='empty log')
                log.append('No log output captured.')
            additional_html.append(log)

    def _appendrow(self, outcome, report):
        result = self.TestResult(outcome, report, self.logfile, self.config)
        if result.row_table is not None:
            index = bisect.bisect_right(self.results, result)
            self.results.insert(index, result)
            tbody = html.tbody(
                result.row_table,
                class_='{0} results-table-row'.format(result.outcome.lower()))
            if result.row_extra is not None:
                tbody.append(result.row_extra)
            self.test_logs.insert(index, tbody)

    def append_passed(self, report):
        if report.when == 'call':
            if hasattr(report, "wasxfail"):
                self.xpassed += 1
                self._appendrow('XPassed', report)
            else:
                self.passed += 1
                self._appendrow('Passed', report)

    def append_failed(self, report):
        if getattr(report, 'when', None) == "call":
            if hasattr(report, "wasxfail"):
                # pytest < 3.0 marked xpasses as failures
                self.xpassed += 1
                self._appendrow('XPassed', report)
            else:
                self.failed += 1
                self._appendrow('Failed', report)
        else:
            self.errors += 1
            self._appendrow('Error', report)

    def append_skipped(self, report):
        if hasattr(report, "wasxfail"):
            self.xfailed += 1
            self._appendrow('XFailed', report)
        else:
            self.skipped += 1
            self._appendrow('Skipped', report)

    def append_other(self, report):
        # For now, the only "other" the plugin give support is rerun
        self.rerun += 1
        self._appendrow('Rerun', report)

    def _generate_report(self, session):
        suite_stop_time = time.time()
        suite_time_delta = suite_stop_time - self.suite_start_time
        numtests = self.passed + self.failed + self.xpassed + self.xfailed
        generated = datetime.datetime.now()

        self.style_css = pkg_resources.resource_string(
            __name__, os.path.join('resources', 'style.css'))
        if PY3:
            self.style_css = self.style_css.decode('utf-8')

        if ANSI:
            ansi_css = [
                '\n/******************************',
                ' * ANSI2HTML STYLES',
                ' ******************************/\n']
            ansi_css.extend([str(r) for r in style.get_styles()])
            self.style_css += '\n'.join(ansi_css)

        css_href = '{0}/{1}'.format('assets', 'style.css')
        html_css = html.link(href=css_href, rel='stylesheet',
                             type='text/css')
        if self.self_contained:
            html_css = html.style(raw(self.style_css))

        head = html.head(
            html.meta(charset='utf-8'),
            html.title('Test Report'),
            html_css)

        class Outcome:

            def __init__(self, outcome, total=0, label=None,
                         test_result=None, class_html=None):
                self.outcome = outcome
                self.label = label or outcome
                self.class_html = class_html or outcome
                self.total = total
                self.test_result = test_result or outcome

                self.generate_checkbox()
                self.generate_summary_item()

            def generate_checkbox(self):
                checkbox_kwargs = {'data-test-result':
                                       self.test_result.lower()}
                if self.total == 0:
                    checkbox_kwargs['disabled'] = 'true'

                self.checkbox = html.input(type='checkbox',
                                           checked='true',
                                           onChange='filter_table(this)',
                                           name='filter_checkbox',
                                           class_='filter',
                                           hidden='true',
                                           **checkbox_kwargs)

            def generate_summary_item(self):
                self.summary_item = html.span('{0} {1}'.
                                              format(self.total, self.label),
                                              class_=self.class_html)

        outcomes = [Outcome('passed', self.passed),
                    Outcome('skipped', self.skipped),
                    Outcome('failed', self.failed),
                    Outcome('error', self.errors, label='errors'),
                    Outcome('xfailed', self.xfailed,
                            label='expected failures'),
                    Outcome('xpassed', self.xpassed,
                            label='unexpected passes')]

        if self.rerun is not None:
            outcomes.append(Outcome('rerun', self.rerun))

        summary = [html.p(
            '{0} tests ran in {1:.2f} seconds. '.format(
                numtests, suite_time_delta)),
            html.p('(Un)check the boxes to filter the results.',
                   class_='filter',
                   hidden='true')]

        for i, outcome in enumerate(outcomes, start=1):
            summary.append(outcome.checkbox)
            summary.append(outcome.summary_item)
            if i < len(outcomes):
                summary.append(', ')

        cells = [
            html.th('Result',
                    class_='sortable result initial-sort',
                    col='result'),
            html.th('Test', class_='sortable', col='name'),
            html.th('Duration', class_='sortable numeric', col='duration'),
            html.th('Links')]
        session.config.hook.pytest_html_results_table_header(cells=cells)

        results = [html.h2('Results'), html.table([html.thead(
            html.tr(cells),
            html.tr([
                html.th('No results found. Try to check the filters',
                        colspan=len(cells))],
                id='not-found-message', hidden='true'),
            id='results-table-head'),
            self.test_logs], id='results-table')]

        main_js = pkg_resources.resource_string(
            __name__, os.path.join('resources', 'main.js'))
        if PY3:
            main_js = main_js.decode('utf-8')

        body = html.body(
            html.script(raw(main_js)),
            html.h1(os.path.basename(session.config.option.htmlpath)),
            html.p('Report generated on {0} at {1} by'.format(
                generated.strftime('%d-%b-%Y'),
                generated.strftime('%H:%M:%S')),
                html.a(' pytest-html', href=__pypi_url__),
                ' v{0}'.format(__version__)),
            onLoad='init()')

        body.extend(self._generate_environment(session.config))

        summary_prefix, summary_postfix = [], []
        session.config.hook.pytest_html_results_summary(
            prefix=summary_prefix, summary=summary, postfix=summary_postfix)
        body.extend([html.h2('Summary')] + summary_prefix
                    + summary + summary_postfix)

        body.extend(results)

        doc = html.html(head, body)

        unicode_doc = u'<!DOCTYPE html>\n{0}'.format(doc.unicode(indent=2))
        if PY3:
            # Fix encoding issues, e.g. with surrogates
            unicode_doc = unicode_doc.encode('utf-8',
                                             errors='xmlcharrefreplace')
            unicode_doc = unicode_doc.decode('utf-8')
        return unicode_doc

    def _generate_environment(self, config):
        if not hasattr(config, '_metadata') or config._metadata is None:
            return []

        metadata = config._metadata
        environment = [html.h2('Environment')]
        rows = []

        for key in [k for k in sorted(metadata.keys()) if metadata[k]]:
            value = metadata[key]
            if isinstance(value, basestring) and value.startswith('http'):
                value = html.a(value, href=value, target='_blank')
            elif isinstance(value, (list, tuple, set)):
                value = ', '.join((str(i) for i in value))
            rows.append(html.tr(html.td(key), html.td(value)))

        environment.append(html.table(rows, id='environment'))
        return environment

    def _save_report(self, report_content):
        dir_name = os.path.dirname(self.logfile)
        assets_dir = os.path.join(dir_name, 'assets')

        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        if not self.self_contained and not os.path.exists(assets_dir):
            os.makedirs(assets_dir)

        with open(self.logfile, 'w', encoding='utf-8') as f:
            f.write(report_content)
        if not self.self_contained:
            style_path = os.path.join(assets_dir, 'style.css')
            with open(style_path, 'w', encoding='utf-8') as f:
                f.write(self.style_css)

    def pytest_runtest_logreport(self, report):
        if report.passed:
            self.append_passed(report)
        elif report.failed:
            self.append_failed(report)
        elif report.skipped:
            self.append_skipped(report)
        else:
            self.append_other(report)

    def pytest_collectreport(self, report):
        if report.failed:
            self.append_failed(report)

    def pytest_sessionstart(self, session):
        self.suite_start_time = time.time()

    def pytest_sessionfinish(self, session):
        report_content = self._generate_report(session)
        self._save_report(report_content)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep('-', 'generated html file: {0}'.format(
            self.logfile))
