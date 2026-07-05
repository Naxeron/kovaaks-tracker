let currentData = { columns: [], rows: [], global_stats: {} };
let sortCol = -1;
let sortAsc = true;
let filteredRows = [];
let renderedRowsCount = 0;
const BATCH_SIZE = 100;
let visibleColumns = null;
let columnWidths = {};
let isResizing = false;
let autoplayActive = false;
let autoplayCurrentScenario = null;
let initialFetchTriggered = false;
let currentAutoHiddenColumns = [];

function getColumnsToRender() {
    if (!currentData || !currentData.columns) return [];
    return currentData.columns.filter(c => {
        if (c === 'Scenario') return true;
        if (currentAutoHiddenColumns.includes(c)) return false;
        if (visibleColumns && !visibleColumns.includes(c)) return false;
        return true;
    });
}

function applyColumnWidths() {
    let styleTag = document.getElementById('dynamic-column-styles');
    if (!styleTag) {
        styleTag = document.createElement('style');
        styleTag.id = 'dynamic-column-styles';
        document.head.appendChild(styleTag);
    }
    
    if (!currentData || !currentData.columns) return;
    
    if (window.currentConfig && window.currentConfig.auto_fit_columns) {
        styleTag.textContent = '';
        return;
    }
    
    const columnsToRender = getColumnsToRender();
    
    let css = '';
    columnsToRender.forEach((col, index) => {
        if (columnWidths[col]) {
            const nth = index + 1;
            const w = columnWidths[col];
            css += `#data-table th:nth-child(${nth}), #data-table td:nth-child(${nth}) { width: ${w}px !important; min-width: ${w}px !important; max-width: ${w}px !important; }\n`;
        }
    });
    styleTag.textContent = css;
}

let filters = {
    losing: false,
    friends: false,
    me: false,
    unplayed: false,
    hidden: false
};

document.addEventListener('DOMContentLoaded', () => {
    // Toggles
    document.querySelectorAll('.toggle-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const filter = e.currentTarget.dataset.filter;
            if (btn.id === 'toggle-hidden') {
                filters.hidden = !filters.hidden;
                btn.classList.toggle('active');
                fetchData();
                return;
            } else {
                filters[filter] = !filters[filter];
                if (filters[filter]) {
                    e.currentTarget.classList.add('active');
                } else {
                    e.currentTarget.classList.remove('active');
                }
            }
            renderTable();
        });
    });

    // Fetch button
    document.getElementById('btn-fetch').addEventListener('click', async () => {
        if (window.pywebview && window.pywebview.api) {
            const cfg = await window.pywebview.api.get_config();
            if (!cfg.username || !cfg.has_password) {
                document.getElementById('login-username').value = cfg.username || '';
                document.getElementById('login-password').value = '';
                document.getElementById('login-show-password').checked = false;
                document.getElementById('login-password').type = 'password';
                document.getElementById('login-modal').style.display = 'flex';
                return;
            }
            startFetch();
        }
    });

    document.getElementById('btn-login-cancel').addEventListener('click', () => {
        document.getElementById('login-modal').style.display = 'none';
    });

    const triggerLoginSubmit = async () => {
        const user = document.getElementById('login-username').value;
        const pass = document.getElementById('login-password').value;
        if (user && pass) {
            document.getElementById('login-modal').style.display = 'none';
            await window.pywebview.api.save_credentials(user, pass);
            startFetch();
        }
    };

    document.getElementById('btn-login-submit').addEventListener('click', triggerLoginSubmit);

    // Accept Enter key on login inputs
    ['login-username', 'login-password'].forEach(id => {
        const inputEl = document.getElementById(id);
        if (inputEl) {
            inputEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    triggerLoginSubmit();
                }
            });
        }
    });

    // Toggle for unhiding password in login prompt
    const loginShowPass = document.getElementById('login-show-password');
    if (loginShowPass) {
        loginShowPass.addEventListener('change', function() {
            const passInput = document.getElementById('login-password');
            if (passInput) {
                passInput.type = this.checked ? 'text' : 'password';
            }
        });
    }

    // Toggle for unhiding password in settings
    const settingsShowPass = document.getElementById('settings-show-password');
    if (settingsShowPass) {
        settingsShowPass.addEventListener('change', function() {
            const passInput = document.getElementById('settings-password');
            if (passInput) {
                passInput.type = this.checked ? 'text' : 'password';
            }
        });
    }

    // Settings logic
    document.getElementById('btn-settings').addEventListener('click', async () => {
        if (window.pywebview && window.pywebview.api) {
            const cfg = await window.pywebview.api.get_config();
            document.getElementById('settings-username').value = cfg.username || '';
            document.getElementById('settings-password').value = cfg.password || '';
            document.getElementById('settings-show-password').checked = false;
            document.getElementById('settings-password').type = 'password';
            document.getElementById('settings-stats-dir').value = cfg.stats_dir || '';
            document.getElementById('settings-min-entries').value = cfg.min_entries || 1000;
            document.getElementById('settings-auto-refresh').checked = cfg.auto_refresh || false;
            document.getElementById('settings-auto-refresh-github').checked = cfg.auto_refresh_github_only || false;
            document.getElementById('settings-refresh-interval').value = cfg.refresh_interval || 60;
            document.getElementById('settings-always-show-total-points').checked = cfg.always_show_total_points !== false;
            document.getElementById('settings-auto-fit-columns').checked = cfg.auto_fit_columns || false;
            document.getElementById('settings-modal').style.display = 'flex';
        }
    });

    document.getElementById('btn-settings-cancel').addEventListener('click', () => {
        document.getElementById('settings-modal').style.display = 'none';
    });

    document.getElementById('btn-settings-submit').addEventListener('click', async () => {
        if (window.pywebview && window.pywebview.api) {
            const username = document.getElementById('settings-username').value;
            const password = document.getElementById('settings-password').value;
            const stats_dir = document.getElementById('settings-stats-dir').value;
            const min_entries = document.getElementById('settings-min-entries').value;
            const auto_refresh = document.getElementById('settings-auto-refresh').checked;
            const auto_refresh_github_only = document.getElementById('settings-auto-refresh-github').checked;
            const refresh_interval = document.getElementById('settings-refresh-interval').value;
            const always_show_total_points = document.getElementById('settings-always-show-total-points').checked;
            const auto_fit_columns = document.getElementById('settings-auto-fit-columns').checked;

            await window.pywebview.api.save_settings({
                username: username,
                password: password,
                stats_dir: stats_dir,
                min_entries: min_entries,
                auto_refresh: auto_refresh,
                auto_refresh_github_only: auto_refresh_github_only,
                refresh_interval: refresh_interval,
                always_show_total_points: always_show_total_points,
                auto_fit_columns: auto_fit_columns
            });
            document.getElementById('settings-modal').style.display = 'none';
            
            // Re-fetch data from backend to update view with new settings like min_entries
            fetchData();
        }
    });



    // Log panel logic
    let logInterval = null;
    let isMouseDownOnLogs = false;

    const logPanel = document.getElementById('log-panel');
    if (logPanel) {
        logPanel.addEventListener('mousedown', () => {
            isMouseDownOnLogs = true;
        });
        document.addEventListener('mouseup', () => {
            isMouseDownOnLogs = false;
        });
    }

    function isSelectingLog() {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) return false;
        const content = document.getElementById('log-content');
        if (!content) return false;
        try {
            const range = sel.getRangeAt(0);
            return content.contains(range.startContainer) || content.contains(range.endContainer);
        } catch (e) {
            return false;
        }
    }

    document.getElementById('btn-log').addEventListener('click', async () => {
        const panel = document.getElementById('log-panel');
        if (panel.style.display === 'none') {
            panel.style.display = 'block';
            await refreshLogs();
            logInterval = setInterval(refreshLogs, 2000);
        } else {
            panel.style.display = 'none';
            if (logInterval) {
                clearInterval(logInterval);
                logInterval = null;
            }
        }
    });

    document.getElementById('btn-clear').addEventListener('click', async () => {
        if (window.pywebview && window.pywebview.api) {
            await window.pywebview.api.clear_logs();
            document.getElementById('log-content').textContent = '';
        }
    });

    async function refreshLogs() {
        if (window.pywebview && window.pywebview.api) {
            const logs = await window.pywebview.api.get_logs();
            const content = document.getElementById('log-content');
            if (!content) return;
            const parent = content.parentElement;
            
            // If the logs are exactly the same, do nothing to prevent unnecessary DOM mutations and selection resets
            if (content.textContent === logs) {
                return;
            }
            
            // If user is selecting or holds mouse down, skip updating logs to preserve selection
            if (isMouseDownOnLogs || isSelectingLog()) {
                return;
            }
            
            const wasAtBottom = (parent.scrollHeight - parent.scrollTop - parent.clientHeight) < 20;
            
            content.textContent = logs;
            
            if (wasAtBottom) {
                parent.scrollTop = parent.scrollHeight;
            }
        }
    }

    document.getElementById('search-input').addEventListener('input', renderTable);
    
    let lastScrollCheck = 0;
    let scrollThrottleTimeout = null;
    const tableContainer = document.querySelector('.table-container');

    let targetScrollTop = tableContainer.scrollTop;
    let currentScrollTop = tableContainer.scrollTop;
    let isAnimatingScroll = false;

    tableContainer.addEventListener('scroll', function() {
        if (!isAnimatingScroll) {
            targetScrollTop = tableContainer.scrollTop;
            currentScrollTop = tableContainer.scrollTop;
        }

        const now = Date.now();
        const checkScroll = () => {
            if (tableContainer.scrollTop + tableContainer.clientHeight >= tableContainer.scrollHeight - 200) {
                renderNextBatch();
            }
        };
        if (now - lastScrollCheck > 50) {
            lastScrollCheck = now;
            window.requestAnimationFrame(checkScroll);
        } else {
            if (scrollThrottleTimeout) clearTimeout(scrollThrottleTimeout);
            scrollThrottleTimeout = setTimeout(() => {
                window.requestAnimationFrame(checkScroll);
            }, 50);
        }
    });

    tableContainer.addEventListener('wheel', function(e) {
        if (e.deltaY === 0) return;
        
        e.preventDefault();
        
        targetScrollTop = Math.max(0, Math.min(
            tableContainer.scrollHeight - tableContainer.clientHeight,
            targetScrollTop + e.deltaY * 0.8
        ));
        
        if (!isAnimatingScroll) {
            animateScroll();
        }
    }, { passive: false });

    function animateScroll() {
        isAnimatingScroll = true;
        const diff = targetScrollTop - currentScrollTop;
        if (Math.abs(diff) < 0.5) {
            tableContainer.scrollTop = targetScrollTop;
            currentScrollTop = targetScrollTop;
            isAnimatingScroll = false;
            return;
        }
        currentScrollTop += diff * 0.15;
        tableContainer.scrollTop = currentScrollTop;
        window.requestAnimationFrame(animateScroll);
    }

    window.resetScrollInterpolation = function() {
        if (tableContainer) {
            targetScrollTop = tableContainer.scrollTop;
            currentScrollTop = tableContainer.scrollTop;
        }
        isAnimatingScroll = false;
    };

    // Context Menu logic
    let contextMenuScenario = null;
    const contextMenu = document.getElementById('context-menu');
    const columnContextMenu = document.getElementById('column-context-menu');
    const logContextMenu = document.getElementById('log-context-menu');
    
    document.getElementById('data-table').addEventListener('contextmenu', (e) => {
        const th = e.target.closest('th');
        if (th) {
            e.preventDefault();
            columnContextMenu.innerHTML = '';
            
            const columns = currentData.columns || [];
            columns.forEach(col => {
                const item = document.createElement('div');
                item.className = 'menu-item';
                
                const isAutoHidden = currentAutoHiddenColumns.includes(col);
                const isVisible = !isAutoHidden && (!visibleColumns || visibleColumns.includes(col) || col === 'Scenario');
                
                let icon;
                if (isAutoHidden) {
                    icon = '&nbsp;&nbsp;&nbsp;';
                } else {
                    icon = isVisible ? '✓ ' : '&nbsp;&nbsp;&nbsp;';
                }
                
                let label = col;
                if (isAutoHidden) {
                    label += ' (filtered)';
                }
                item.innerHTML = `<span style="display:inline-block; width:15px;">${icon}</span> ${label}`;
                
                if (col === 'Scenario' || isAutoHidden) {
                    item.style.color = '#888';
                    item.style.cursor = 'not-allowed';
                } else {
                    item.addEventListener('click', async () => {
                        if (!visibleColumns) {
                            visibleColumns = [...columns];
                        }
                        
                        if (isVisible) {
                            visibleColumns = visibleColumns.filter(c => c !== col);
                        } else {
                            visibleColumns.push(col);
                        }
                        
                        if (window.pywebview && window.pywebview.api) {
                            await window.pywebview.api.save_settings({
                                visible_columns: visibleColumns
                            });
                        }
                        
                        columnContextMenu.style.display = 'none';
                        renderTable();
                    });
                }
                columnContextMenu.appendChild(item);
            });
            
            columnContextMenu.style.display = 'block';
            columnContextMenu.style.left = e.pageX + 'px';
            columnContextMenu.style.top = e.pageY + 'px';
            
            contextMenu.style.display = 'none';
            if (logContextMenu) logContextMenu.style.display = 'none';
            return;
        }

        const tr = e.target.closest('tr');
        if (tr && tr.parentElement.tagName === 'TBODY') {
            e.preventDefault();
            const td = tr.firstElementChild;
            // The scenario name is the text content of the first td (omitting the ▶ if present)
            // It might have HTML like <span ...>▶</span> Scenario Name
            const scenarioName = td.textContent.replace('▶', '').trim();
            contextMenuScenario = scenarioName;
            
            contextMenu.style.display = 'block';
            contextMenu.style.left = e.pageX + 'px';
            contextMenu.style.top = e.pageY + 'px';
            
            columnContextMenu.style.display = 'none';
            inputContextMenu.style.display = 'none';
            if (logContextMenu) logContextMenu.style.display = 'none';
        }
    });

    let activeInput = null;
    const inputContextMenu = document.getElementById('input-context-menu');

    document.addEventListener('contextmenu', (e) => {
        const input = e.target.closest('input');
        if (input && (input.type === 'text' || input.type === 'password' || input.type === 'number')) {
            e.preventDefault();
            activeInput = input;
            
            // Hide other context menus
            contextMenu.style.display = 'none';
            columnContextMenu.style.display = 'none';
            if (logContextMenu) logContextMenu.style.display = 'none';
            
            // Show input context menu
            inputContextMenu.style.display = 'block';
            inputContextMenu.style.left = e.pageX + 'px';
            inputContextMenu.style.top = e.pageY + 'px';
        }
    });

    document.addEventListener('click', (e) => {
        if (!contextMenu.contains(e.target)) {
            contextMenu.style.display = 'none';
        }
        if (!columnContextMenu.contains(e.target)) {
            columnContextMenu.style.display = 'none';
        }
        if (!inputContextMenu.contains(e.target)) {
            inputContextMenu.style.display = 'none';
        }
        if (logContextMenu && !logContextMenu.contains(e.target)) {
            logContextMenu.style.display = 'none';
        }
    });

    document.getElementById('menu-input-copy').addEventListener('click', () => {
        if (activeInput) {
            const start = activeInput.selectionStart;
            const end = activeInput.selectionEnd;
            const selectedText = activeInput.value.substring(start, end);
            if (selectedText) {
                navigator.clipboard.writeText(selectedText);
            }
        }
        inputContextMenu.style.display = 'none';
    });

    document.getElementById('menu-input-cut').addEventListener('click', () => {
        if (activeInput) {
            const start = activeInput.selectionStart;
            const end = activeInput.selectionEnd;
            const text = activeInput.value;
            const selectedText = text.substring(start, end);
            if (selectedText) {
                navigator.clipboard.writeText(selectedText);
                activeInput.value = text.substring(0, start) + text.substring(end);
                activeInput.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }
        inputContextMenu.style.display = 'none';
    });

    document.getElementById('menu-input-paste').addEventListener('click', async () => {
        if (activeInput) {
            try {
                const clipboardText = await navigator.clipboard.readText();
                const start = activeInput.selectionStart;
                const end = activeInput.selectionEnd;
                const text = activeInput.value;
                activeInput.value = text.substring(0, start) + clipboardText + text.substring(end);
                
                // Position cursor after pasted text
                const newCursorPos = start + clipboardText.length;
                activeInput.setSelectionRange(newCursorPos, newCursorPos);
                
                // Trigger input event to update model
                activeInput.dispatchEvent(new Event('input', { bubbles: true }));
            } catch (err) {
                console.error("Failed to read clipboard using navigator.clipboard: ", err);
                if (window.pywebview && window.pywebview.api && window.pywebview.api.get_clipboard) {
                    try {
                        const clipboardText = await window.pywebview.api.get_clipboard();
                        const start = activeInput.selectionStart;
                        const end = activeInput.selectionEnd;
                        const text = activeInput.value;
                        activeInput.value = text.substring(0, start) + clipboardText + text.substring(end);
                        const newCursorPos = start + clipboardText.length;
                        activeInput.setSelectionRange(newCursorPos, newCursorPos);
                        activeInput.dispatchEvent(new Event('input', { bubbles: true }));
                    } catch (pyErr) {
                        console.error("Failed to read clipboard via python bridge: ", pyErr);
                    }
                }
            }
        }
        inputContextMenu.style.display = 'none';
    });

    document.getElementById('menu-input-selectall').addEventListener('click', () => {
        if (activeInput) {
            activeInput.select();
        }
        inputContextMenu.style.display = 'none';
    });

    // Log Context Menu logic
    if (logPanel && logContextMenu) {
        logPanel.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            
            // Hide other context menus
            contextMenu.style.display = 'none';
            columnContextMenu.style.display = 'none';
            inputContextMenu.style.display = 'none';
            
            // Enable/disable copy depending on whether there's selection
            const copyItem = document.getElementById('menu-log-copy');
            if (copyItem) {
                if (isSelectingLog()) {
                    copyItem.style.opacity = '1';
                    copyItem.style.pointerEvents = 'auto';
                } else {
                    copyItem.style.opacity = '0.5';
                    copyItem.style.pointerEvents = 'none';
                }
            }
            
            logContextMenu.style.display = 'block';
            logContextMenu.style.left = e.pageX + 'px';
            logContextMenu.style.top = e.pageY + 'px';
        });

        const logCopyBtn = document.getElementById('menu-log-copy');
        if (logCopyBtn) {
            logCopyBtn.addEventListener('click', () => {
                const sel = window.getSelection();
                if (sel) {
                    const selectedText = sel.toString();
                    if (selectedText) {
                        navigator.clipboard.writeText(selectedText);
                    }
                }
                logContextMenu.style.display = 'none';
            });
        }

        const logSelectAllBtn = document.getElementById('menu-log-selectall');
        if (logSelectAllBtn) {
            logSelectAllBtn.addEventListener('click', () => {
                const content = document.getElementById('log-content');
                if (content) {
                    const range = document.createRange();
                    range.selectNodeContents(content);
                    const sel = window.getSelection();
                    if (sel) {
                        sel.removeAllRanges();
                        sel.addRange(range);
                    }
                }
                logContextMenu.style.display = 'none';
            });
        }
    }

    document.getElementById('menu-play-scenario').addEventListener('click', () => {
        if (contextMenuScenario && window.pywebview && window.pywebview.api) {
            if (autoplayActive) {
                autoplayCurrentScenario = contextMenuScenario;
                window.pywebview.api.update_status(`Autoplay: launching '${contextMenuScenario}' — waiting for score…`);
            }
            window.pywebview.api.play_scenario(contextMenuScenario);
        }
        contextMenu.style.display = 'none';
    });

    document.getElementById('menu-copy-scenario').addEventListener('click', () => {
        if (contextMenuScenario) {
            navigator.clipboard.writeText(contextMenuScenario);
            contextMenu.style.display = 'none';
        }
    });

    document.getElementById('menu-toggle-hide').addEventListener('click', async () => {
        if (contextMenuScenario && window.pywebview && window.pywebview.api) {
            await window.pywebview.api.toggle_hide_scenario(contextMenuScenario);
            contextMenu.style.display = 'none';
            fetchData(); // Reload data to reflect hidden status
        }
    });

    // Play button click handler
    document.getElementById('data-table').addEventListener('click', (e) => {
        const playBtn = e.target.closest('.play-btn-cell');
        if (playBtn) {
            const scenario = playBtn.getAttribute('data-scenario');
            if (scenario && window.pywebview && window.pywebview.api) {
                if (autoplayActive) {
                    autoplayCurrentScenario = scenario;
                    window.pywebview.api.update_status(`Autoplay: launching '${scenario}' — waiting for score…`);
                }
                window.pywebview.api.play_scenario(scenario);
            }
        }
    });

    // Select row on click
    document.getElementById('data-table').addEventListener('click', (e) => {
        const tr = e.target.closest('tr');
        if (tr && tr.parentElement.tagName === 'TBODY') {
            document.querySelectorAll('#data-table tbody tr').forEach(r => r.classList.remove('selected'));
            tr.classList.add('selected');
            
            // If autoplay is active, update the tracked scenario to this one
            if (autoplayActive) {
                const td = tr.firstElementChild;
                const scenarioName = td.textContent.replace('▶', '').trim();
                autoplayCurrentScenario = scenarioName;
                if (window.pywebview && window.pywebview.api) {
                    window.pywebview.api.update_status(`Autoplay ON — waiting for score on: ${autoplayCurrentScenario}`);
                }
            }
        }
    });

    // Double click on row to play scenario
    document.getElementById('data-table').addEventListener('dblclick', (e) => {
        const tr = e.target.closest('tr');
        if (tr && tr.parentElement.tagName === 'TBODY') {
            const td = tr.firstElementChild;
            const scenarioName = td.textContent.replace('▶', '').trim();
            if (scenarioName && window.pywebview && window.pywebview.api) {
                if (autoplayActive) {
                    autoplayCurrentScenario = scenarioName;
                    window.pywebview.api.update_status(`Autoplay: launching '${scenarioName}' — waiting for score…`);
                }
                window.pywebview.api.play_scenario(scenarioName);
            }
        }
    });

    // Autoplay toggle button handler
    document.getElementById('btn-autoplay').addEventListener('click', () => {
        autoplayActive = !autoplayActive;
        const btn = document.getElementById('btn-autoplay');
        if (autoplayActive) {
            btn.classList.add('active');
            
            // Set autoplay scenario: look for selected row, otherwise first row
            const selectedTr = document.querySelector('#data-table tbody tr.selected');
            let targetTr = selectedTr;
            if (!targetTr) {
                targetTr = document.querySelector('#data-table tbody tr');
                if (targetTr) {
                    targetTr.classList.add('selected');
                }
            }
            
            if (targetTr) {
                const td = targetTr.firstElementChild;
                autoplayCurrentScenario = td.textContent.replace('▶', '').trim();
                if (window.pywebview && window.pywebview.api) {
                    window.pywebview.api.update_status(`Autoplay ON — waiting for score on: ${autoplayCurrentScenario}`);
                }
            } else {
                autoplayCurrentScenario = null;
                if (window.pywebview && window.pywebview.api) {
                    window.pywebview.api.update_status("Autoplay ON — select or play a scenario to begin");
                }
            }
        } else {
            btn.classList.remove('active');
            autoplayCurrentScenario = null;
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.update_status("Autoplay OFF");
            }
        }
    });

    window.addEventListener('pywebviewready', function() {
        fetchData();
    });
});

let autoRefreshTimer = null;
function setupAutoRefresh(cfg) {
    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
    }
    if (cfg.auto_refresh) {
        const interval = parseInt(cfg.refresh_interval) || 60;
        autoRefreshTimer = setInterval(() => {
            startFetch();
        }, interval * 60 * 1000);
    }
}

async function startFetch() {
    setLoading(true, "Fetching stats from Steam...");
    try {
        await window.pywebview.api.fetch_all_stats();
    } catch (err) {
        console.error(err);
        setStatus("Error fetching stats.");
    }
}

async function fetchData() {
    setLoading(true, "Loading data...");
    if (window.pywebview && window.pywebview.api) {
        try {
            const cfg = await window.pywebview.api.get_config();
            window.currentConfig = cfg;
            setupAutoRefresh(cfg);
            visibleColumns = cfg.visible_columns;
            columnWidths = cfg.column_widths || {};

            const showHidden = document.getElementById('toggle-hidden').classList.contains('active');
            currentData = await window.pywebview.api.get_data(cfg.min_entries || 1000, showHidden);
            window.zombies = new Set(currentData.zombies || []);
            
            if (cfg.username && !initialFetchTriggered) {
                initialFetchTriggered = true;
                if (!cfg.has_password) {
                    setTimeout(() => {
                        document.getElementById('login-username').value = cfg.username;
                        document.getElementById('login-password').value = '';
                        document.getElementById('login-show-password').checked = false;
                        document.getElementById('login-password').type = 'password';
                        document.getElementById('login-modal').style.display = 'flex';
                    }, 500);
                } else {
                    startFetch();
                }
            }
            
            renderTable();
            setStatus("Ready");
        } catch (err) {
            console.error(err);
            setStatus("Error loading data.");
        }
    } else {
        // Mock data
        currentData = {
            columns: ["Scenario", "Entry Count", "My Rank", "My Score", "Friend Rank", "Friend Score"],
            rows: [
                ["Tile Frenzy", "50000", "1200", "120", "100", "150"],
                ["Ascended Tracking", "12000", "", "", "500", "14000"]
            ],
            global_stats: { points: 1000, potential_points: 2000, projected_gain: 500, total_rows: 2 }
        };
        renderTable();
    }
    setLoading(false);
}

function renderTable() {
    const table = document.getElementById('data-table');
    const thead = table.querySelector('thead tr');
    const tbody = table.querySelector('tbody');
    
    thead.innerHTML = '';
    tbody.innerHTML = '';
    renderedRowsCount = 0;
    if (window.resetScrollInterpolation) window.resetScrollInterpolation();

    if (!currentData.columns || currentData.columns.length === 0) return;

    if (sortCol === -1) {
        const entryIndex = currentData.columns.indexOf("Entry Count");
        if (entryIndex !== -1) {
            sortCol = entryIndex;
            sortAsc = false;
        }
    }

    // 1. Filter and sort rows first
    filteredRows = [...currentData.rows];
    
    const searchTerm = document.getElementById('search-input').value.toLowerCase();

    const colIndex = {};
    currentData.columns.forEach((c, i) => colIndex[c] = i);

    filteredRows = filteredRows.filter(row => {
        if (searchTerm) {
            const matchesSearch = row.some(cell => String(cell).toLowerCase().includes(searchTerm));
            if (!matchesSearch) return false;
        }
        
        const myRank = row[colIndex["My Rank"]];
        const topFriend = row[colIndex["Top Friend"]];
        
        const isPlayedByMe = myRank !== "";
        const isPlayedByFriend = topFriend !== "";
        const rankDiff = parseInt(row[colIndex["Rank Diff"]]);

        const isLosing = isPlayedByMe && isPlayedByFriend && !isNaN(rankDiff) && rankDiff > 0;
        const isFriendsOnly = isPlayedByFriend && !isPlayedByMe;
        const isMeOnly = isPlayedByMe && !isPlayedByFriend;
        const isUnplayed = !isPlayedByMe && !isPlayedByFriend;

        const anyFilterActive = filters.losing || filters.friends || filters.me || filters.unplayed;
        if (anyFilterActive) {
            let pass = false;
            if (filters.losing && isLosing) pass = true;
            if (filters.friends && isFriendsOnly) pass = true;
            if (filters.me && isMeOnly) pass = true;
            if (filters.unplayed && isUnplayed) pass = true;
            if (!pass) return false;
        }

        return true;
    });

    if (sortCol !== -1) {
        filteredRows.sort((a, b) => {
            let valA = a[sortCol];
            let valB = b[sortCol];
            
            if (typeof valA === 'string' && typeof valB === 'string') {
                const numA = parseFloat(valA.replace(/,/g, '').replace(/%/g, '').replace(/\+/g, ''));
                const numB = parseFloat(valB.replace(/,/g, '').replace(/%/g, '').replace(/\+/g, ''));
                if (!isNaN(numA) && !isNaN(numB)) {
                    valA = numA;
                    valB = numB;
                }
            }

            if (valA < valB) return sortAsc ? -1 : 1;
            if (valA > valB) return sortAsc ? 1 : -1;
            return 0;
        });
    }

    // 2. Compute currentAutoHiddenColumns
    currentAutoHiddenColumns = [];
    if (filteredRows.length > 0) {
        currentData.columns.forEach(col => {
            if (col === 'Scenario') return;
            const idx = colIndex[col];
            let isEmpty = true;
            for (let i = 0; i < filteredRows.length; i++) {
                const val = filteredRows[i][idx];
                if (val !== undefined && val !== null && String(val).trim() !== "") {
                    isEmpty = false;
                    break;
                }
            }
            if (isEmpty) {
                currentAutoHiddenColumns.push(col);
            }
        });
    }

    // 3. Render headers based on getColumnsToRender()
    const columnsToRender = getColumnsToRender();

    columnsToRender.forEach((col) => {
        const originalIndex = currentData.columns.indexOf(col);
        const th = document.createElement('th');
        
        const labelStr = col + (originalIndex === sortCol ? (sortAsc ? ' ▲' : ' ▼') : '');
        
        const labelSpan = document.createElement('span');
        labelSpan.textContent = labelStr;
        labelSpan.style.display = 'block';
        labelSpan.style.overflow = 'hidden';
        labelSpan.style.textOverflow = 'ellipsis';
        th.appendChild(labelSpan);
        
        th.addEventListener('click', (e) => {
            if (isResizing) return;
            if (sortCol === originalIndex) {
                sortAsc = !sortAsc;
            } else {
                sortCol = originalIndex;
                sortAsc = true;
            }
            renderTable();
        });
        
        if (!window.currentConfig || !window.currentConfig.auto_fit_columns) {
            const resizer = document.createElement('div');
            resizer.classList.add('resizer');
            th.appendChild(resizer);
            
            let startX = 0;
            let startW = 0;
            const mouseDownHandler = function (e) {
                e.preventDefault(); // prevent text selection
                e.stopPropagation(); // prevent sort
                startX = e.clientX;
                startW = th.offsetWidth;
                isResizing = true;
                
                document.addEventListener('mousemove', mouseMoveHandler);
                document.addEventListener('mouseup', mouseUpHandler);
                resizer.classList.add('resizing');
            };
            const mouseMoveHandler = function (e) {
                const dx = e.clientX - startX;
                const newWidth = Math.max(30, startW + dx);
                columnWidths[col] = newWidth;
                applyColumnWidths();
            };
            const mouseUpHandler = function () {
                document.removeEventListener('mousemove', mouseMoveHandler);
                document.removeEventListener('mouseup', mouseUpHandler);
                resizer.classList.remove('resizing');
                if (window.pywebview && window.pywebview.api) {
                    window.pywebview.api.save_settings({ column_widths: columnWidths });
                }
                setTimeout(() => {
                    isResizing = false;
                }, 50);
            };
            resizer.addEventListener('mousedown', mouseDownHandler);
            resizer.addEventListener('click', (e) => e.stopPropagation());
        }

        thead.appendChild(th);
    });

    if (!window.currentConfig || !window.currentConfig.auto_fit_columns) {
        const fillerTh = document.createElement('th');
        fillerTh.style.width = '100%';
        fillerTh.style.borderRight = 'none';
        fillerTh.style.cursor = 'default';
        thead.appendChild(fillerTh);
    }

    applyColumnWidths();

    // 4. Update stats and trigger batch rendering
    const alwaysShow = window.currentConfig ? window.currentConfig.always_show_total_points : true;
    if (currentData.global_stats) {
        if (alwaysShow !== false) {
            document.getElementById('stat-points').textContent = currentData.global_stats.points.toLocaleString();
            document.getElementById('stat-potential').textContent = currentData.global_stats.potential_points.toLocaleString();
            document.getElementById('stat-next-rank').textContent = 'Loading...';
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.get_next_rank_points().then(res => {
                    document.getElementById('stat-next-rank').textContent = res;
                });
            }
        } else {
            let pts = 0;
            let pot = 0;
            filteredRows.forEach(row => {
                const entries = parseInt(row[colIndex["Entry Count"]]) || 0;
                const myRank = parseInt(row[colIndex["My Rank"]]);
                if (!isNaN(myRank)) {
                    pts += (entries - myRank);
                    pot += (myRank - 1);
                } else if (entries > 0) {
                    pot += (entries - 1);
                }
            });
            document.getElementById('stat-points').textContent = pts.toLocaleString();
            document.getElementById('stat-potential').textContent = pot.toLocaleString();
            document.getElementById('stat-next-rank').textContent = "+?";
        }
        document.getElementById('stat-rows').textContent = filteredRows.length + ' rows';
    }

    renderNextBatch();
}

function renderNextBatch() {
    if (renderedRowsCount >= filteredRows.length) return;
    
    const tbody = document.querySelector('#data-table tbody');
    const endIndex = Math.min(renderedRowsCount + BATCH_SIZE, filteredRows.length);
    
    const fragment = document.createDocumentFragment();
    
    const columnsToRender = getColumnsToRender();
    
    for (let i = renderedRowsCount; i < endIndex; i++) {
        const row = filteredRows[i];
        const tr = document.createElement('tr');
        
        const scenarioIndex = currentData.columns.indexOf("Scenario");
        const scenarioName = row[scenarioIndex];
        if (window.zombies && window.zombies.has(scenarioName)) {
            tr.classList.add('zombie-row');
        }
        
        columnsToRender.forEach((col) => {
            const originalIndex = currentData.columns.indexOf(col);
            const cell = row[originalIndex];
            const td = document.createElement('td');
            if (originalIndex === 0) {
                const escapedCell = String(cell).replace(/"/g, '&quot;');
                td.innerHTML = `<span class="play-btn-cell" data-scenario="${escapedCell}" style="color:#aaaaaa; margin-right:5px; cursor:pointer;">▶</span> ${cell}`;
            } else {
                td.textContent = cell;
            }
            tr.appendChild(td);
        });
        
        if (!window.currentConfig || !window.currentConfig.auto_fit_columns) {
            const fillerTd = document.createElement('td');
            fillerTd.style.borderRight = 'none';
            tr.appendChild(fillerTd);
        }
        
        fragment.appendChild(tr);
    }
    
    tbody.appendChild(fragment);
    renderedRowsCount = endIndex;
}

function setStatus(text) {
    document.getElementById('status-text').textContent = text;
}

function setLoading(isLoading, text = "") {
    const bar = document.getElementById('progress-bar');
    if (isLoading) {
        setStatus(text);
        bar.style.width = '100%';
        bar.style.animation = 'pulse 1.5s infinite';
    } else {
        bar.style.animation = 'none';
        bar.style.width = '0%';
    }
}

window.updateProgress = function(current, total, message) {
    const bar = document.getElementById('progress-bar');
    if (total > 0) {
        const pct = (current / total) * 100;
        bar.style.width = pct + '%';
    }
    if (message) {
        setStatus(message);
    }
};

window.fetchData = fetchData;
window.setStatus = setStatus;

window.onLocalScoreDetected = function(scenarioName) {
    if (autoplayActive && autoplayCurrentScenario && scenarioName === autoplayCurrentScenario) {
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.update_status(`Autoplay: local score detected for '${scenarioName}', advancing…`);
        }
        autoplayAdvance();
    }
};

window.onZombieDetected = function(scenarioName) {
    if (autoplayActive && autoplayCurrentScenario && scenarioName === autoplayCurrentScenario) {
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.update_status(`Autoplay: '${scenarioName}' is a zombie scenario, advancing…`);
        }
        autoplayAdvance();
    }
};

function autoplayAdvance() {
    if (!autoplayActive) return;
    if (filteredRows.length === 0) {
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.update_status("Autoplay: no scenarios in list");
        }
        return;
    }
    
    const colIndex = {};
    currentData.columns.forEach((c, i) => colIndex[c] = i);
    const scenarioCol = colIndex["Scenario"];
    
    let currentIdx = -1;
    for (let i = 0; i < filteredRows.length; i++) {
        if (filteredRows[i][scenarioCol] === autoplayCurrentScenario) {
            currentIdx = i;
            break;
        }
    }
    
    if (currentIdx === -1) {
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.update_status(`Autoplay: '${autoplayCurrentScenario}' not found in current view`);
        }
        return;
    }
    
    let nextIdx = currentIdx + 1;
    while (nextIdx < filteredRows.length) {
        const nextScenario = filteredRows[nextIdx][scenarioCol];
        if (window.zombies && window.zombies.has(nextScenario)) {
            nextIdx++;
        } else {
            break;
        }
    }
    if (nextIdx >= filteredRows.length) {
        autoplayActive = false;
        const btn = document.getElementById('btn-autoplay');
        btn.classList.remove('active');
        autoplayCurrentScenario = null;
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.update_status("Autoplay: reached end of list, autoplay disabled");
        }
        return;
    }
    
    const nextScenario = filteredRows[nextIdx][scenarioCol];
    autoplayCurrentScenario = nextScenario;
    
    selectRowByName(nextScenario);
    
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.update_status(`Autoplay: launching '${nextScenario}' — waiting for score…`);
        window.pywebview.api.play_scenario(nextScenario);
    }
}

function selectRowByName(name) {
    document.querySelectorAll('#data-table tbody tr').forEach(r => r.classList.remove('selected'));
    
    const rows = document.querySelectorAll('#data-table tbody tr');
    let found = false;
    for (let tr of rows) {
        const td = tr.firstElementChild;
        if (td && td.textContent.replace('▶', '').trim() === name) {
            tr.classList.add('selected');
            tr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            found = true;
            break;
        }
    }
    
    if (!found) {
        while (renderedRowsCount < filteredRows.length) {
            const oldLength = document.querySelectorAll('#data-table tbody tr').length;
            renderNextBatch();
            const newRows = document.querySelectorAll('#data-table tbody tr');
            for (let i = oldLength; i < newRows.length; i++) {
                const tr = newRows[i];
                const td = tr.firstElementChild;
                if (td && td.textContent.replace('▶', '').trim() === name) {
                    tr.classList.add('selected');
                    tr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                    found = true;
                    break;
                }
            }
            if (found) break;
        }
    }
}
