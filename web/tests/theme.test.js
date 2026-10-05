import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import { initializeTheme } from '../src/theme.js';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const bootstrap = html.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/)[1];

function page({ saved = null, denyRead = false, denyWrite = false } = {}) {
  const root = { dataset: { theme: 'dark' }, style: {} };
  const selector = new EventTarget();
  selector.value = 'dark';
  const themeColor = { content: '#1c1e1c' };
  const writes = [];
  const view = {
    get localStorage() {
      if (denyRead) throw new Error('Storage access denied');
      return {
        getItem: key => key === 'meridian-theme' ? saved : null,
        setItem(key, value) {
          if (denyWrite) throw new Error('Storage write denied');
          writes.push([key, value]);
        },
      };
    },
    getComputedStyle: () => ({ backgroundColor: root.dataset.theme === 'light' ? 'rgb(244, 242, 235)' : 'rgb(28, 30, 28)' }),
  };
  const doc = {
    documentElement: root,
    defaultView: view,
    querySelector: query => query === '#appearance' ? selector : themeColor,
  };
  runInNewContext(bootstrap, { document: doc, window: view });
  const beforeInit = root.dataset.theme;
  initializeTheme(doc);
  return {
    root, selector, themeColor, writes, beforeInit,
    choose(value) {
      selector.value = value;
      selector.dispatchEvent(new Event('change'));
    },
  };
}

test('saved light appearance is applied before initialization and reflected by the control', () => {
  const state = page({ saved: 'light' });
  assert.equal(state.beforeInit, 'light');
  assert.equal(state.selector.value, 'light');
  assert.equal(state.root.style.colorScheme, 'light');
  assert.equal(state.themeColor.content, 'rgb(244, 242, 235)');
  assert.deepEqual(state.writes, []);
});

test('absent, dark and unsupported stored preferences start dark', () => {
  for (const saved of [null, 'dark', '', 'system', 'LIGHT', '{"theme":"light"}']) {
    const state = page({ saved });
    assert.equal(state.beforeInit, 'dark');
    assert.equal(state.selector.value, 'dark');
    assert.equal(state.root.style.colorScheme, 'dark');
    assert.equal(state.themeColor.content, 'rgb(28, 30, 28)');
  }
});

test('changing appearance updates the root, browser color and saved preference', () => {
  const state = page();
  state.choose('light');
  assert.equal(state.root.dataset.theme, 'light');
  assert.equal(state.themeColor.content, 'rgb(244, 242, 235)');
  state.choose('dark');
  assert.equal(state.root.dataset.theme, 'dark');
  assert.equal(state.root.style.colorScheme, 'dark');
  assert.equal(state.themeColor.content, 'rgb(28, 30, 28)');
  assert.deepEqual(state.writes, [['meridian-theme', 'light'], ['meridian-theme', 'dark']]);
});

test('blocked storage access keeps dark startup and permits changing appearance', () => {
  const state = page({ denyRead: true });
  assert.equal(state.beforeInit, 'dark');
  state.choose('light');
  assert.equal(state.root.dataset.theme, 'light');
  assert.equal(state.selector.value, 'light');
  assert.equal(state.themeColor.content, 'rgb(244, 242, 235)');
});

test('a failed preference write leaves the requested appearance active', () => {
  const state = page({ saved: 'light', denyWrite: true });
  state.choose('dark');
  assert.equal(state.root.dataset.theme, 'dark');
  assert.equal(state.selector.value, 'dark');
  assert.equal(state.root.style.colorScheme, 'dark');
  assert.deepEqual(state.writes, []);
});
