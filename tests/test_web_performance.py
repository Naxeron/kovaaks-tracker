"""Exercise browser scheduling and sorting without a GUI or production data."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "web" / "script.js"

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const listeners = new Map();
const timers = new Map();
let nextTimer = 1;

class Element {
    constructor() {
        this.style = {};
        this.children = [];
        this.listeners = new Map();
        this.value = '';
        this.textContent = '';
        this.scrollTop = 0;
        const classes = new Set();
        this.classList = {
            add: value => classes.add(value),
            remove: value => classes.delete(value),
            contains: value => classes.has(value),
            toggle(value, active) { active ? classes.add(value) : classes.delete(value); }
        };
    }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute() {}
    set innerHTML(value) { this.children = []; }
    querySelector(selector) { return element(selector); }
}
const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
}
const context = vm.createContext({
    assert,
    console: { error() {} },
    timers,
    document: {
        head: new Element(),
        addEventListener(name, fn) { listeners.set(name, fn); },
        getElementById: element,
        querySelector: element,
        querySelectorAll() { return []; },
        createElement() { return new Element(); },
        createDocumentFragment() { return new Element(); },
        createTextNode(text) { const node = new Element(); node.textContent = text; return node; }
    },
    window: { addEventListener() {}, currentConfig: { auto_fit_columns: true } },
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return nextTimer++; },
    clearInterval() {},
    ready() { listeners.get('DOMContentLoaded')(); }
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext(`
    function deferred() {
        let resolve, reject;
        const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
        return {promise, resolve, reject};
    }
    async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
    function data(name) {
        return {columns: ['Scenario', 'Entry Count', 'My Rank', 'Top Friend', 'Rank Diff'],
            rows: [[name, '1000', '50', '', '']],
            global_stats: {points: 950, potential_points: 49}, zombies: []};
    }
`, context);
const test = fs.readFileSync(0, 'utf8');
vm.runInContext(`(async () => { ${test} })()`, context).then(
    () => process.stdout.write('ok'),
    err => { process.stderr.write(err.stack); process.exitCode = 1; }
);
"""


def run_browser_test(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser performance regressions")
    result = subprocess.run(
        [node, "-e", NODE_HARNESS, str(SCRIPT_PATH)],
        input=source,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_sort_order_active_column_cache_and_dataset_invalidation():
    run_browser_test(r"""
        const values = ['10', '2', '-3.5', '+4.25%', '1,234', 'Alpha', 'beta',
            '', null, undefined, NaN, 5, '  ', 'alpha', '2026-01-01', '-0.5',
            '+.75', '3.', '1e3'];
        currentData = data('unused');
        currentData.rows = values.map((value, index) => [String(index), value, '', '', '']);
        const originalRows = currentData.rows.slice();
        const originalKey = getSortKey;
        let keyCalls = 0;
        getSortKey = value => { keyCalls++; return originalKey(value); };
        sortCol = 1;
        const names = () => getFilteredAndSortedRows(false).map(row => row[0]).join(',');
        assert.strictEqual(names(), '2,15,16,1,17,3,11,0,4,18,14,5,13,6,7,8,9,10,12');
        assert.strictEqual(keyCalls, values.length);
        sortAsc = false;
        assert.strictEqual(names(), '6,5,13,14,18,4,0,11,3,17,1,16,15,2,7,8,9,10,12');
        document.getElementById('search-input').value = 'alpha';
        assert.strictEqual(names(), '5,13');
        assert.strictEqual(keyCalls, values.length);
        assert(currentData.rows.every((row, index) => row === originalRows[index]));
        sortCol = 0;
        names();
        assert.strictEqual(keyCalls, 2 * values.length);
        sortCol = 1;
        names();
        // Returning to an older column rebuilds its evicted keys; repeated
        // sorting/searching within that active column still reuses them.
        assert.strictEqual(keyCalls, 3 * values.length);
        sortAsc = true;
        assert.strictEqual(names(), '5,13');
        assert.strictEqual(keyCalls, 3 * values.length);
        document.getElementById('search-input').value = '';
        currentData = data('new dataset');
        assert.strictEqual(names(), 'new dataset');
        assert.strictEqual(keyCalls, 3 * values.length + 1);
        currentData.rows = [['replacement', '2', '', '', '']];
        assert.strictEqual(names(), 'replacement');
        assert.strictEqual(keyCalls, 3 * values.length + 2);
    """)


def test_search_debounces_and_autoplay_flushes_pending_search_immediately():
    run_browser_test(r"""
        ready();
        currentData = data('first');
        currentData.rows.push(['second', '2000', '60', '', '']);
        let renders = 0;
        const originalRender = renderTable;
        renderTable = () => { renders++; originalRender(); };
        const input = document.getElementById('search-input');
        const onInput = input.listeners.get('input');
        input.value = 'f'; onInput();
        input.value = 'fi'; onInput();
        input.value = ''; onInput();
        assert.strictEqual(renders, 0);
        assert.strictEqual(timers.size, 1);
        const [timer, pending] = [...timers.entries()][0];
        assert.strictEqual(pending.delay, 120);
        timers.delete(timer);
        pending.fn();
        assert.strictEqual(renders, 1);
        sortCol = 1;
        sortAsc = true;
        renderTable();
        onInput();
        autoplayActive = true;
        autoplayCurrentScenario = 'first';
        const launched = [];
        window.pywebview = {api: {update_status() {}, play_scenario(name) { launched.push(name); }}};
        selectRowByName = () => {};
        window.onLocalScoreDetected('first');
        assert.strictEqual(launched.join(','), 'second');
        assert.strictEqual(timers.size, 0);
        assert.strictEqual(searchRenderTimer, null);
        assert.strictEqual(renders, 3);
    """)


def test_empty_data_refresh_releases_previous_sort_cache():
    run_browser_test(r"""
        currentData = data('old dataset');
        sortCol = 1;
        getFilteredAndSortedRows(false);
        assert.strictEqual(sortKeyCache.size, 1);
        window.pywebview = {api: {
            async get_config() { return {username: ''}; },
            async is_fetch_in_progress() { return false; },
            async get_data() { return {columns: [], rows: [], global_stats: {}}; }
        }};
        await fetchData(true);
        // Empty tables return before sorting, so replacement must still
        // release the old map and its references to the previous dataset.
        assert.strictEqual(sortKeyCache.size, 0);
        assert.strictEqual(sortKeyData, null);
        assert.strictEqual(sortKeyRows, null);
        assert.strictEqual(sortKeyColumns, null);
    """)


@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("loading_mode", ["silent", "visible-first", "visible-queued"])
def test_concurrent_refreshes_coalesce_and_recover(first_fails, loading_mode):
    run_browser_test(
        f"const firstFails = {json.dumps(first_fails)}; "
        f"const loadingMode = {json.dumps(loading_mode)}; " + r"""
        const requests = [];
        const loading = [];
        const rendered = [];
        const statuses = [];
        let active = 0;
        let maxActive = 0;
        window.pywebview = {api: {
            async get_config() { return {username: '', min_entries: 1000}; },
            async is_fetch_in_progress() { return false; },
            get_data(minimum, hidden) {
                const next = deferred();
                requests.push({next, minimum, hidden});
                maxActive = Math.max(maxActive, ++active);
                return next.promise.finally(() => active--);
            },
            async get_next_rank_points() { return '+100'; },
            async get_scenarios_left_to_next_rank() { return {count: '2'}; }
        }};
        setLoading = busy => loading.push(busy);
        setStatus = status => statuses.push(status);
        renderTable = () => rendered.push(currentData.rows[0][0]);
        const first = fetchData(loadingMode !== 'visible-first');
        await flush();
        assert.strictEqual(requests.length, 1);
        assert.strictEqual(loading.join(','), loadingMode === 'visible-first' ? 'true' : '');
        document.getElementById('toggle-hidden').classList.add('active');
        for (let i = 0; i < 30; i++) {
            assert.strictEqual(fetchData(loadingMode !== 'visible-queued' || i !== 10), first);
        }
        const expectedBusy = loadingMode === 'silent' ? '' : 'true';
        const expectedDone = loadingMode === 'silent' ? '' : 'true,false';
        assert.strictEqual(loading.join(','), expectedBusy);
        if (firstFails) requests[0].next.reject(new Error('network failed'));
        else requests[0].next.resolve(data('old'));
        await flush();
        assert.strictEqual(requests.length, 2);
        assert.strictEqual(requests[1].hidden, true);
        assert.strictEqual(maxActive, 1);
        assert.strictEqual(loading.join(','), expectedBusy);
        requests[1].next.resolve(data('latest'));
        await first;
        assert.strictEqual(rendered.join(','), firstFails ? 'latest' : 'old,latest');
        assert.strictEqual(loading.join(','), expectedDone);
        assert.strictEqual(statuses.at(-1), 'Ready');
        assert.strictEqual(activeDataFetch, null);
        const retry = fetchData(true);
        await flush();
        assert.strictEqual(requests.length, 3);
        requests[2].next.reject(new Error('another failure'));
        await retry;
        assert.strictEqual(statuses.at(-1), 'Error loading data.');
        assert.strictEqual(loading.join(','), expectedDone);
        assert.strictEqual(activeDataFetch, null);
        const afterFailure = fetchData();
        await flush();
        requests[3].next.resolve(data('recovered'));
        await afterFailure;
        assert.strictEqual(rendered.at(-1), 'recovered');
        assert.strictEqual(loading.join(','), expectedDone ? expectedDone + ',true,false' : 'true,false');
    """)


def test_rank_stats_refresh_only_for_data_and_ignore_stale_responses():
    run_browser_test(r"""
        const ranks = [];
        let leftCalls = 0;
        window.pywebview = {api: {
            async get_config() { return {username: '', auto_fit_columns: true}; },
            async is_fetch_in_progress() { return false; },
            async get_data() { return data('scenario'); },
            get_next_rank_points() { const next = deferred(); ranks.push(next); return next.promise; },
            async get_scenarios_left_to_next_rank() { leftCalls++; return {count: '2', live_gap: '+75'}; }
        }};
        await fetchData(true);
        assert.strictEqual(ranks.length, 1);
        renderTable();
        sortAsc = !sortAsc;
        renderTable();
        document.getElementById('search-input').value = 'scenario';
        renderTable();
        assert.strictEqual(ranks.length, 1);
        await fetchData(true);
        assert.strictEqual(ranks.length, 2);
        ranks[1].resolve('+75');
        await flush();
        ranks[0].resolve('+999');
        await flush();
        assert.strictEqual(document.getElementById('stat-next-rank').textContent, '+75');
        assert.strictEqual(document.getElementById('stat-live-gap').textContent, '+75');
        assert.strictEqual(leftCalls, 1);
        renderTable();
        assert.strictEqual(document.getElementById('stat-next-rank').textContent, '+75');
        const pending = refreshGlobalRankStats();
        window.currentConfig.always_show_total_points = false;
        renderTable();
        await refreshGlobalRankStats();
        ranks[2].resolve('+888');
        await pending;
        assert.strictEqual(document.getElementById('stat-next-rank').textContent, '+?');
        assert.strictEqual(leftCalls, 1);
        window.currentConfig.always_show_total_points = true;
        const failed = refreshGlobalRankStats();
        ranks[3].reject(new Error('rank unavailable'));
        await failed;
        assert.strictEqual(document.getElementById('stat-next-rank').textContent, 'N/A');
    """)


def test_rank_background_update_invalidates_older_bridge_response():
    run_browser_test(r"""
        currentData = data('scenario');
        window.currentConfig = {username: 'player', always_show_total_points: true};
        const ranks = [];
        let leftCalls = 0;
        window.pywebview = {api: {
            get_next_rank_points() { const next = deferred(); ranks.push(next); return next.promise; },
            async get_scenarios_left_to_next_rank() {
                leftCalls++;
                return {count: '2', live_gap: '+75'};
            }
        }};
        const stale = refreshGlobalRankStats();
        assert.strictEqual(ranks.length, 1);
        await window.onRankStatsUpdated('other player');
        assert.strictEqual(ranks.length, 1);
        window.currentConfig.always_show_total_points = false;
        await window.onRankStatsUpdated('player');
        assert.strictEqual(ranks.length, 1);
        window.currentConfig.always_show_total_points = true;
        const fresh = window.onRankStatsUpdated('player');
        assert.strictEqual(ranks.length, 2);
        ranks[1].resolve('+75');
        await fresh;
        ranks[0].resolve('+999');
        await stale;
        assert.strictEqual(document.getElementById('stat-next-rank').textContent, '+75');
        assert.strictEqual(document.getElementById('stat-live-gap').textContent, '+75');
        assert.strictEqual(leftCalls, 1);
    """)
