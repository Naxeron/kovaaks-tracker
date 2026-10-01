"""Exercise the real credential UI with an isolated bridge and minimal DOM."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


WEB_DIR = Path(__file__).resolve().parents[1] / "web"
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = fs.readFileSync(0, 'utf8');
const elements = new Map();
class Element {
    constructor() {
        this.value = '';
        this.type = 'password';
        this.textContent = '';
        this.placeholder = '';
        this.disabled = false;
        this.style = {display: 'none'};
        this.listeners = {};
        this.children = new Map();
        this.classList = {add() {}, remove() {}, contains() {return false;}};
    }
    addEventListener(event, callback) {this.listeners[event] = callback;}
    fire(event, detail = {}) {return this.listeners[event]?.call(this, detail);}
    querySelector(selector) {
        if (!this.children.has(selector)) this.children.set(selector, new Element());
        return this.children.get(selector);
    }
}
const html = fs.readFileSync(process.argv[2], 'utf8');
for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element());
const e = id => {
    if (!elements.has(id)) throw new Error('Missing HTML element: ' + id);
    return elements.get(id);
};
let ready;
const calls = {fetch: 0, reload: 0, saves: []};
const cfg = {username: 'alice', has_password: true, credential_storage: 'saved'};
// Even a faulty backend returning this property must not cause it to be read.
Object.defineProperty(cfg, 'password', {get() {throw new Error('Read raw password');}});
const api = {
    get_config: async () => cfg,
    save_credentials: async (...args) => {
        calls.saves.push(args);
        return {ok: true, credential_storage: 'saved'};
    },
    save_settings: async args => {
        calls.saves.push(args);
        return {ok: true, credential_storage: 'saved'};
    },
    clear_credentials: async () => ({ok: true, credential_storage: 'empty'})
};
const context = vm.createContext({
    e, api, cfg, calls,
    document: {
        addEventListener(event, callback) {if (event === 'DOMContentLoaded') ready = callback;},
        getElementById: e,
        querySelectorAll() {return [];},
        querySelector() {return new Element();}
    },
    window: {pywebview: {api}, addEventListener() {}},
    setTimeout() {},
    console: {error() {throw new Error('Credential errors must not be logged');}}
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext(`
    const originalFetchData = fetchData;
    startFetch = () => {calls.fetch += 1;};
    fetchData = () => {calls.reload += 1;};
    function snapshot(prefix) {
        return {
            password: e(prefix + '-password').value,
            type: e(prefix + '-password').type,
            display: e(prefix + '-modal').style.display,
            disabled: e('btn-' + prefix + '-submit').disabled,
            message: e(prefix + '-credential-message').textContent
        };
    }
`, context);
ready();
vm.runInContext('(async () => {' + scenario + '})()', context).then(result => {
    process.stdout.write(JSON.stringify(result));
}).catch(error => {
    process.stderr.write(String(error));
    process.exitCode = 1;
});
"""


def run_ui(scenario):
    """Run only JavaScript and an in-memory bridge; no OS credentials are touched."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the JavaScript runtime regression")
    result = subprocess.run(
        [node, "-e", HARNESS, str(WEB_DIR / "script.js"), str(WEB_DIR / "index.html")],
        input=scenario,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_settings_keeps_password_in_backend_and_clears_cancelled_input():
    result = run_ui("""
        await e('btn-settings').fire('click');
        const opened = snapshot('settings');
        const sameUserPlaceholder = e('settings-password').placeholder;
        e('settings-username').value = 'bob';
        e('settings-username').fire('input');
        const otherUserPlaceholder = e('settings-password').placeholder;
        e('settings-password').value = 'typed-secret';
        e('settings-show-password').fire('click');
        e('btn-settings-cancel').fire('click');
        return {opened, sameUserPlaceholder, otherUserPlaceholder, closed: snapshot('settings')};
    """)
    assert result["opened"]["password"] == ""
    assert result["opened"]["display"] == "flex"
    assert result["sameUserPlaceholder"] == "Leave blank to keep current password"
    assert result["otherUserPlaceholder"] == "Enter password"
    assert result["closed"]["password"] == ""
    assert result["closed"]["type"] == "password"
    assert result["closed"]["display"] == "none"


@pytest.mark.parametrize("prefix", ["login", "settings"])
def test_session_only_saves_keep_visible_notice_after_closing(prefix):
    result = run_ui(f"""
        const prefix = {json.dumps(prefix)};
        if (prefix === 'settings') await e('btn-settings').fire('click');
        else showLoginModal('alice');
        const method = prefix === 'settings' ? 'save_settings' : 'save_credentials';
        api[method] = async () => ({{ok: true, credential_storage: 'session',
            message: 'Password is available for this session only.'}});
        e(prefix + '-password').value = 'new-secret';
        e(prefix + '-show-password').fire('click');
        await e('btn-' + prefix + '-submit').fire('click');
        setStatus('Ready');
        return {{state: snapshot(prefix), notice: e('credential-notice').textContent,
            noticeDisplay: e('credential-notice').style.display, calls}};
    """)
    assert result["state"]["password"] == ""
    assert result["state"]["type"] == "password"
    assert result["state"]["display"] == "none"
    assert not result["state"]["disabled"]
    assert "this session only" in result["notice"]
    assert result["noticeDisplay"] == "block"
    assert result["calls"]["fetch" if prefix == "login" else "reload"] == 1


@pytest.mark.parametrize("prefix", ["login", "settings"])
@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_failed_saves_keep_modal_open_and_do_not_expose_error(prefix, failure):
    result = run_ui(f"""
        const prefix = {json.dumps(prefix)};
        if (prefix === 'settings') await e('btn-settings').fire('click');
        else showLoginModal('alice');
        const method = prefix === 'settings' ? 'save_settings' : 'save_credentials';
        api[method] = async () => {{
            if ({json.dumps(failure)} === 'exception') throw new Error('sensitive-backend-detail');
            return {{ok: false, message: 'Please unlock the credential store and try again.'}};
        }};
        e(prefix + '-password').value = 'new-secret';
        await e('btn-' + prefix + '-submit').fire('click');
        return {{state: snapshot(prefix), calls}};
    """)
    assert result["state"]["password"] == ""
    assert result["state"]["display"] == "flex"
    assert not result["state"]["disabled"]
    assert result["state"]["message"]
    assert "sensitive-backend-detail" not in result["state"]["message"]
    assert result["calls"]["fetch"] == result["calls"]["reload"] == 0


@pytest.mark.parametrize("failure", [False, True])
def test_forget_password_reports_result_and_keeps_settings_open(failure):
    result = run_ui(f"""
        await e('btn-settings').fire('click');
        e('settings-password').value = 'typed-secret';
        api.clear_credentials = async () => ({{ok: {json.dumps(not failure)},
            message: {json.dumps('Unable to remove password.' if failure else 'Password removed.')},
            credential_storage: {json.dumps('saved' if failure else 'empty')}}});
        await e('btn-forget-password').fire('click');
        return {{state: snapshot('settings'), placeholder: e('settings-password').placeholder,
            forgetDisabled: e('btn-forget-password').disabled}};
    """)
    assert result["state"]["password"] == ""
    assert result["state"]["display"] == "flex"
    assert not result["state"]["disabled"]
    assert not result["forgetDisabled"]
    assert result["placeholder"] == (
        "Leave blank to keep current password" if failure else "Enter password"
    )
    assert result["state"]["message"] == (
        "Unable to remove password." if failure else "Password removed."
    )


def test_pending_login_blocks_duplicate_submit_and_cancel():
    result = run_ui("""
        showLoginModal('alice');
        e('login-password').value = 'typed-secret';
        let resolveSave;
        let savedCount = 0;
        api.save_credentials = () => {
            savedCount += 1;
            return new Promise(resolve => {resolveSave = resolve;});
        };
        const pending = e('btn-login-submit').fire('click');
        const submitDisabled = e('btn-login-submit').disabled;
        const cancelDisabled = e('btn-login-cancel').disabled;
        e('login-password').fire('keydown', {key: 'Enter'});
        resolveSave({ok: true, credential_storage: 'saved'});
        await pending;
        return {savedCount, submitDisabled, cancelDisabled, state: snapshot('login')};
    """)
    assert result["savedCount"] == 1
    assert result["submitDisabled"] and result["cancelDisabled"]
    assert result["state"]["password"] == ""
    assert not result["state"]["disabled"]


def test_saving_other_settings_sends_blank_password_for_backend_to_retain():
    result = run_ui("""
        await e('btn-settings').fire('click');
        await e('btn-settings-submit').fire('click');
        return calls.saves[0];
    """)
    assert result["username"] == "alice"
    assert result["password"] == ""


def test_login_cancel_clears_visible_password():
    result = run_ui("""
        showLoginModal('alice');
        e('login-password').value = 'typed-secret';
        e('login-show-password').fire('click');
        e('btn-login-cancel').fire('click');
        return snapshot('login');
    """)
    assert result["password"] == ""
    assert result["type"] == "password"
    assert result["display"] == "none"


def test_forget_cannot_remove_another_accounts_password_after_username_edit():
    result = run_ui("""
        await e('btn-settings').fire('click');
        let cleared = 0;
        api.clear_credentials = async () => {cleared += 1; return {ok: true};};
        e('settings-username').value = 'bob';
        e('settings-username').fire('input');
        const disabledForBob = e('btn-forget-password').disabled;
        await e('btn-forget-password').fire('click');
        e('settings-username').value = 'alice';
        e('settings-username').fire('input');
        return {disabledForBob, cleared, disabledForAlice: e('btn-forget-password').disabled};
    """)
    assert result["disabledForBob"]
    assert result["cleared"] == 0
    assert not result["disabledForAlice"]


@pytest.mark.parametrize("storage", ["unavailable", "saved"])
def test_startup_preserves_credential_warnings_across_fetch_status_updates(storage):
    result = run_ui(f"""
        cfg.credential_storage = {json.dumps(storage)};
        cfg.credential_warning = true;
        cfg.credential_message = 'Please check credential settings.';
        api.is_fetch_in_progress = async () => false;
        api.get_data = async () => ({{columns: [], rows: [], zombies: []}});
        setLoading = () => {{}};
        renderTable = () => {{}};
        await originalFetchData();
        return {{notice: e('credential-notice').textContent,
            display: e('credential-notice').style.display, status: e('status-text').textContent}};
    """)
    assert result["notice"] == "Please check credential settings."
    assert result["display"] == "block"
    assert result["status"] == "Ready"


def test_forget_transport_error_restores_controls_without_leaking_details():
    result = run_ui("""
        await e('btn-settings').fire('click');
        e('settings-password').value = 'typed-secret';
        api.clear_credentials = async () => {throw new Error('sensitive-backend-detail');};
        await e('btn-forget-password').fire('click');
        return {state: snapshot('settings'), forgetDisabled: e('btn-forget-password').disabled};
    """)
    assert result["state"]["password"] == ""
    assert result["state"]["display"] == "flex"
    assert not result["state"]["disabled"]
    assert not result["forgetDisabled"]
    assert result["state"]["message"] == "Unable to remove the saved password. Please try again."


def test_forget_updates_session_state_when_saved_password_deletion_fails():
    result = run_ui("""
        await e('btn-settings').fire('click');
        api.clear_credentials = async () => ({
            ok: false, has_password: false, credential_storage: 'unavailable',
            credential_warning: true,
            message: 'Session cleared, but saved password may remain in the credential store.'
        });
        await e('btn-forget-password').fire('click');
        return {state: snapshot('settings'), placeholder: e('settings-password').placeholder,
            notice: e('credential-notice').textContent,
            noticeDisplay: e('credential-notice').style.display};
    """)
    assert result["state"]["display"] == "flex"
    assert result["placeholder"] == "Enter password"
    assert result["state"]["message"].startswith("Session cleared, but saved password may remain")
    assert result["notice"] == result["state"]["message"]
    assert result["noticeDisplay"] == "block"
