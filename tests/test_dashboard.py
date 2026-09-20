import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def check_zoom(page):
    zoom_out = page.get_by_role('button', name='Zoom out', exact=True)
    assert zoom_out.is_enabled(), 'Zoom out must work below 100%'
    zoom_out.click()
    assert page.locator('#zoom-level').inner_text() == '75%'
    zoom_out.click()
    zoom_out.click()
    assert page.locator('#zoom-level').inner_text() == '25%'
    assert zoom_out.is_disabled()
    width = page.locator('#diagram').bounding_box()['width']
    assert abs(width / page.locator('#diagram-pane').bounding_box()['width'] - .25) < .01
    page.get_by_role('button', name='Fit', exact=True).click()
    assert page.locator('#zoom-level').inner_text() == 'Fit'
    assert zoom_out.is_enabled()
    for _ in range(16):
        page.get_by_role('button', name='Zoom in', exact=True).click()
    assert page.locator('#zoom-level').inner_text() == '500%'
    assert page.get_by_role('button', name='Zoom in', exact=True).is_disabled()
    page.get_by_role('button', name='Fit', exact=True).click()


def check_hide_tasks(page, report_path):
    diagram = page.locator('#diagram-pane')
    original_height = diagram.bounding_box()['height']
    stage = page.get_by_role('button', name='InfraVM', exact=False)
    stage.click()
    panel = page.locator('#task-panel')
    results = page.locator('#task-results')
    assert panel.is_visible()
    assert diagram.bounding_box()['height'] < original_height
    assert results.evaluate('e => e.scrollHeight > e.clientHeight'), 'Task results must scroll within their pane'
    page.get_by_role('button', name='Hide task results').click()
    assert panel.is_hidden()
    assert page.locator('#detail').inner_text() == ''
    assert abs(diagram.bounding_box()['height'] - original_height) < 2
    assert stage.get_attribute('aria-pressed') == 'false'
    assert stage.evaluate('e => e === document.activeElement')
    report = json.loads(report_path.read_text())
    report['updated_at'] += 1
    report.update(state='failed', current='nfs')
    report['stages']['nfs']['state'] = 'failed'
    report_path.write_text(json.dumps(report))
    page.locator('#run-state').get_by_text('Failed', exact=True).wait_for()
    assert panel.is_hidden(), 'Progress updates must not reopen hidden task results'
    assert abs(diagram.bounding_box()['height'] - original_height) < 2
    stage.click()
    assert panel.is_visible()
    assert page.locator('#task-title').inner_text() == 'InfraVM: task results'
    page.get_by_role('button', name='Hide task results').focus()
    page.keyboard.press('Enter')
    assert panel.is_hidden()
    page.get_by_role('button', name='NFS VM', exact=False).click()
    assert panel.is_visible()
    assert page.locator('#task-title').inner_text() == 'NFS VM: task results'


def test_dashboard():
    with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
        environment = {name: value for name, value in os.environ.items() if not name.startswith('SOPHON_')}
        server = subprocess.Popen(
            [sys.executable, str(ROOT / 'deployment.py'), '--serve', '--state-dir', temporary],
            env=environment, stdout=subprocess.PIPE, text=True)
        try:
            url = server.stdout.readline().strip()
            assert url.startswith('http://127.0.0.1:'), 'Dashboard did not start'
            with urlopen(url + 'status.json', timeout=5) as response:
                report = json.load(response)
            report.update(state='completed', run_id='browser-fixture')
            for stage in ('infravm', 'nfs'):
                report['stages'][stage].update(state='completed', ok=40, tasks=[
                    {'id': str(index), 'name': f'Task {index}', 'state': 'ok', 'action': 'debug', 'source': 'fixture.yml'}
                    for index in range(40)
                ])
            report_path = Path(temporary) / 'status.json'
            report_path.write_text(json.dumps(report))
            browser = playwright.chromium.launch()
            try:
                for width, height in ((1440, 1000), (390, 844)):
                    report_path.write_text(json.dumps(report))
                    page = browser.new_page(viewport={'width': width, 'height': height}, color_scheme='light')
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.locator('#configure:not([disabled])').wait_for()
                    assert page.get_by_role('separator', include_hidden=True).count() == 0
                    check_zoom(page)
                    check_hide_tasks(page, report_path)
                    assert not errors, errors
                    page.close()
            finally:
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=5)
            server.stdout.close()


if __name__ == '__main__':
    test_dashboard()
    print('Dashboard browser checks passed')
