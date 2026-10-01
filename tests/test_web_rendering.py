"""Run the real table renderer with a minimal DOM to check untrusted names."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "kovaaks" / "web" / "script.js"

# No browser or third-party JavaScript packages are needed. The DOM rejects
# innerHTML writes, so the regression catches unsafe interpolation directly.
RENDER_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const name = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
    constructor(tagName) {
        this.tagName = tagName;
        this.childNodes = [];
        this.attributes = {};
        this.style = {};
        this.classList = { add() {} };
        this.value = '';
    }
    appendChild(child) {
        if (child.tagName === '#fragment') {
            this.childNodes.push(...child.childNodes);
        } else {
            this.childNodes.push(child);
        }
        return child;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    set textContent(value) { this.value = String(value); this.childNodes = []; }
    get textContent() {
        return this.value + this.childNodes.map(child => child.textContent).join('');
    }
    set innerHTML(value) {
        throw new Error('Scenario names must not be inserted as HTML: ' + value);
    }
}

const tbody = new Element('TBODY');
const launched = [];
const context = vm.createContext({
    document: {
        addEventListener() {},
        querySelector(selector) {
            if (selector !== '#data-table tbody') throw new Error(selector);
            return tbody;
        },
        createElement(tag) { return new Element(tag.toUpperCase()); },
        createDocumentFragment() { return new Element('#fragment'); },
        createTextNode(text) {
            const node = new Element('#text');
            node.textContent = text;
            return node;
        }
    },
    window: { pywebview: { api: { play_scenario(name) { launched.push(name); } } } }
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.scenarioName = name;
vm.runInContext(`
    currentData = { columns: ['Scenario', 'My Score'], rows: [[scenarioName, '123.4']] };
    filteredRows = currentData.rows;
    renderNextBatch();
`, context);

const row = tbody.childNodes[0];
const cell = row.childNodes[0];
const button = cell.childNodes[0];
context.playScenario(button.getAttribute('data-scenario'));
process.stdout.write(JSON.stringify({
    text: cell.textContent,
    tags: cell.childNodes.map(child => child.tagName),
    buttonClass: button.className,
    scenarioAttribute: button.getAttribute('data-scenario'),
    scoreText: row.childNodes[1].textContent,
    launched
}));
"""


@pytest.mark.parametrize("scenario_name", [
    'Tile Frenzy',
    'A & B "quoted" <fast>',
    'Scenario &quot; &#9654; ▶',
    '<img src=x onerror="window.pywebview.api.clear_logs()">',
])
def test_scenario_names_are_literal_text_and_launch_unchanged(scenario_name):
    """Rendering must preserve the exact name without interpreting its markup."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the JavaScript runtime regression")

    result = subprocess.run(
        [node, "-e", RENDER_HARNESS, str(SCRIPT_PATH)],
        input=json.dumps(scenario_name),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    rendered = json.loads(result.stdout)

    assert rendered["text"] == f"▶ {scenario_name}"
    assert rendered["tags"] == ["SPAN", "#text"]
    assert rendered["buttonClass"] == "play-btn-cell"
    assert rendered["scenarioAttribute"] == scenario_name
    assert rendered["scoreText"] == "123.4"
    assert rendered["launched"] == [scenario_name]
