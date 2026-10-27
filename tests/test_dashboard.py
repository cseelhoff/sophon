import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import urlopen

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    raise unittest.SkipTest('Playwright and Chromium are required for the browser test')


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


def check_hide_tasks(page, report_path, step):
    diagram = page.locator('#diagram-pane')
    original_height = diagram.bounding_box()['height']
    first, second = ('InfraVM', 'Proxmox authentication') if step == 'provision' else ('Portainer', 'CoreDNS')
    second_key = 'proxmox' if step == 'provision' else 'coredns'
    stage = page.get_by_role('button', name=first, exact=False)
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
    report.update(state='failed', current=second_key)
    report['stages'][second_key]['state'] = 'failed'
    report_path.write_text(json.dumps(report))
    page.locator('#run-state').get_by_text('Failed', exact=True).wait_for()
    assert panel.is_hidden(), 'Progress updates must not reopen hidden task results'
    assert abs(diagram.bounding_box()['height'] - original_height) < 2
    stage.click()
    assert panel.is_visible()
    assert page.locator('#task-title').inner_text() == f'{first}: task results'
    page.get_by_role('button', name='Hide task results').focus()
    page.keyboard.press('Enter')
    assert panel.is_hidden()
    page.get_by_role('button', name=second, exact=False).click()
    assert panel.is_visible()
    assert page.locator('#task-title').inner_text() == f'{second}: task results'


def check_cloudflare_help(page):
    page.get_by_role('button', name='Configure', exact=True).click()
    token = page.locator('input[name="SOPHON_CF_TOKEN"]')
    token.wait_for()
    domain = page.locator('input[name="SOPHON_DOMAIN"]')
    assert domain.is_visible() and domain.get_attribute('required') is not None
    assert domain.input_value() == ''
    assert token.get_attribute('type') == 'password'
    assert token.get_attribute('aria-describedby') == 'cloudflare-token-help'
    certificate = page.locator('textarea[name="SOPHON_ACME_PAYLOAD"]')
    assert certificate.is_visible()
    assert certificate.get_attribute('maxlength') == str(2 * 1024 * 1024)
    assert certificate.get_attribute('autocomplete') == 'off'
    assert certificate.get_attribute('required') is None
    certificate.fill('X' * 120000)
    assert len(certificate.input_value()) == 120000
    certificate.fill('')
    help_text = page.locator('#cloudflare-token-help')
    assert help_text.is_visible()
    link = help_text.get_by_role('link')
    assert link.get_attribute('href') == 'https://dash.cloudflare.com/profile/api-tokens'
    assert link.get_attribute('rel') == 'noopener noreferrer'
    rows = help_text.locator('tbody tr')
    assert [row.locator('td').all_text_contents() for row in rows.all()] == [
        ['Zone', 'Zone', 'Read'], ['Zone', 'DNS', 'Edit'], ['Account', 'Cloudflare Tunnel', 'Edit'],
    ]
    assert help_text.bounding_box()['y'] >= token.bounding_box()['y'] + token.bounding_box()['height']
    page.get_by_role('button', name='Close', exact=True).click()


def check_run_confirmation(page, step):
    page.get_by_role('button', name='Configure', exact=True).click()
    page.locator('#deploy-button:not([disabled])').wait_for()
    for field in page.locator('#settings-fields input[required]:enabled').all():
        if not field.input_value():
            field.fill('24' if field.get_attribute('type') == 'number' else 'TEST_NOT_A_REAL_SECRET')
    messages = []

    def cancel(dialog):
        messages.append(dialog.message)
        dialog.dismiss()

    page.once('dialog', cancel)
    page.get_by_role('button', name='Run', exact=True).click()
    assert len(messages) == 1 and 'undefined' not in messages[0], messages
    assert ('No hypervisor access is used.' in messages[0]) == (step == 'configure')
    page.get_by_role('button', name='Close', exact=True).click()


def check_vcenter_settings(page, report_path):
    page.get_by_role('button', name='Configure', exact=True).click()
    provider = page.locator('select[name="SOPHON_HYPERVISOR"]')
    provider.select_option('vcenter')
    proxmox = page.locator('input[name="SOPHON_PROXMOX_PASSWORD"]')
    assert proxmox.is_hidden() and proxmox.is_disabled()
    assert page.locator('input[name="SOPHON_VNET_INTERFACE"]').is_hidden()
    values = {
        'SOPHON_VCENTER_URL': 'https://vcenter.fixture.example',
        'SOPHON_VCENTER_PASSWORD': 'FIXTURE_MANAGEMENT_SECRET',
        'SOPHON_VCENTER_DATACENTER': 'DC',
        'SOPHON_VCENTER_CLUSTER': 'Cluster',
        'SOPHON_VCENTER_DATASTORE': 'Shared',
        'SOPHON_VCENTER_NETWORK': 'VLAN51',
    }
    for name, value in values.items():
        field = page.locator(f'input[name="{name}"]')
        assert field.is_visible() and field.is_enabled()
        field.fill(value)
    page.get_by_role('button', name='Generate shell deployment command', exact=True).click()
    command = page.locator('#shell-command')
    command.wait_for(state='visible')
    assert 'SOPHON_HYPERVISOR=vcenter' in command.input_value()
    assert 'https://vcenter.fixture.example' in command.input_value()
    page.get_by_role('button', name='Close', exact=True).click()
    report = json.loads(report_path.read_text())
    report['hypervisor'] = 'vcenter'
    report['updated_at'] += 1
    report['stages']['vcenter'].update(state='completed', ok=1)
    report_path.write_text(json.dumps(report))
    page.get_by_role('button', name='vCenter authentication', exact=False).wait_for()
    assert page.get_by_role('button', name='Proxmox authentication', exact=False).count() == 0
    page.get_by_role('button', name='Configure', exact=True).click()
    provider.select_option('proxmox')
    assert page.locator('input[name="SOPHON_PROXMOX_PASSWORD"]').is_enabled()
    page.get_by_role('button', name='Close', exact=True).click()
    report['hypervisor'] = 'proxmox'
    report['updated_at'] += 1
    report_path.write_text(json.dumps(report))
    page.get_by_role('button', name='Proxmox authentication', exact=False).wait_for()


def check_startup_error(page, report_path, step):
    report = json.loads(report_path.read_text())
    report.update(state='failed', current=None, exit_code=127)
    for stage in report['stages'].values():
        stage.update(state='pending', tasks=[])
    report_path.write_text(json.dumps(report))
    page.reload()
    error = page.locator('#run-error')
    error.get_by_text('Deployment failed before any stage started.', exact=False).wait_for()
    assert 'Exit code: 127' in error.inner_text()
    assert str(report_path.parent / 'deploy.log') in error.inner_text()
    page.get_by_role('button', name='Prepare media' if step == 'provision' else 'Portainer', exact=False).click()
    assert error.is_visible()
    assert page.locator('#task-results').inner_text() == ''
    report['error'] = 'Could not start ansible-playbook. Restart the viewer with the sophon-tools container.'
    report_path.write_text(json.dumps(report))
    error.get_by_text(report['error'], exact=False).wait_for()
    page.wait_for_timeout(1200)
    assert report['error'] in error.inner_text(), 'Polling must not clear the failure message'
    report.update(state='running', exit_code=0)
    report.pop('error')
    report_path.write_text(json.dumps(report))
    error.wait_for(state='hidden')


def test_dashboard(step):
    with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
        environment = {name: value for name, value in os.environ.items() if not name.startswith('SOPHON_')}
        if step == 'configure':
            environment['SOPHON_DEPLOYMENT_STEP'] = 'configure'
        server = subprocess.Popen(
            [sys.executable, str(ROOT / 'src/deployment.py'), '--step', step, '--serve', '--state-dir', temporary],
            env=environment, stdout=subprocess.PIPE, text=True)
        try:
            url = server.stdout.readline().strip()
            assert url.startswith('http://127.0.0.1:'), 'Dashboard did not start'
            with urlopen(url + 'status.json', timeout=5) as response:
                report = json.load(response)
            report.update(state='completed', run_id='browser-fixture')
            for stage in ('infravm', 'proxmox', 'coredns'):
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
                    if step == 'provision':
                        assert 'sudo bash /mnt/sophon/install.sh' in page.locator('#step-note').inner_text()
                    else:
                        assert 'No hypervisor credentials' in page.locator('#step-note').inner_text()
                    check_hide_tasks(page, report_path, step)
                    if step == 'configure':
                        check_cloudflare_help(page)
                    else:
                        page.get_by_role('button', name='Configure', exact=True).click()
                        page.locator('input[name="SOPHON_INFRAVM_DISK_GB"]').wait_for()
                        assert page.locator('input[name="SOPHON_CONTROLLER_PASSWORD"]').count() == 0
                        assert page.locator('input[name="SOPHON_INFRAVM_IP"]').count() == 0
                        assert page.locator('input[name="SOPHON_CF_TOKEN"]').count() == 0
                        assert page.locator('input[name="SOPHON_DOMAIN"]').count() == 0
                        page.get_by_role('button', name='Close', exact=True).click()
                    check_run_confirmation(page, step)
                    if step == 'provision':
                        check_vcenter_settings(page, report_path)
                    check_startup_error(page, report_path, step)
                    assert not errors, errors
                    page.close()
            finally:
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=5)
            server.stdout.close()


if __name__ == '__main__':
    for step in ('provision', 'configure'):
        test_dashboard(step)
    print('Dashboard browser checks passed')
