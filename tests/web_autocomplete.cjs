// Run the application's rendered script against a small event/DOM boundary.
// Fetch intentionally ignores abort: late completions must still be harmless.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {script, elements: markup} = JSON.parse(fs.readFileSync(0, 'utf8'));
const decode = text => text.replaceAll('&quot;', '"').replaceAll('&amp;', '&');
class Element {
  constructor() {
    this.listeners = {}; this.value = ''; this.hidden = true;
    this.dataset = {}; this.checked = true; this.selectionStart = null;
    this.innerHTML = '';
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async fire(type, fields = {}) {
    const event = {target: this, preventDefault() { this.defaultPrevented = true; }, ...fields};
    for (const listener of this.listeners[type] || []) await listener(event);
    return event;
  }
  set innerHTML(text) {
    this.html = text;
    this.buttons = [...text.matchAll(/data-tag="([^"]*)"/g)].map(match => ({
      dataset: {tag: decode(match[1])},
      active: false,
      classList: {toggle(_, value) { this.owner.active = value; }},
    }));
    for (const button of this.buttons) button.classList.owner = button;
  }
  get innerHTML() { return this.html; }
  querySelectorAll() { return this.buttons; }
  focus() {}
  setSelectionRange(start) { this.selectionStart = start; }
  closest() { return null; }
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settled() { await tick(); await tick(); }
function setup() {
  const elements = new Map(), pending = [], timers = new Map();
  let timer = 0;
  const get = selector => {
    if (elements.has(selector)) return elements.get(selector);
    const attr = selector.match(/^(\w+)\[name=['"]([^'"]+)['"]\]$/);
    const found = markup.find(node => selector.startsWith('#')
      ? node.attrs.id === selector.slice(1)
      : attr && node.tag === attr[1] && node.attrs.name === attr[2]);
    if (!found) return null;
    const element = new Element();
    elements.set(selector, element);
    return element;
  };
  const document = new Element();
  document.querySelector = get;
  const window = new Element();
  window.location = {};
  const context = vm.createContext({
    document, window, AbortController,
    setTimeout: fn => { timers.set(++timer, fn); return timer; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, {signal}) => new Promise(resolve => pending.push({
      url, signal,
      respond(tags, ok = true) { resolve({ok, json: async () => tags}); },
      respondDeferredJSON() {
        let finish;
        const json = new Promise(resolve => { finish = resolve; });
        resolve({ok: true, json: () => json});
        return finish;
      },
    })),
  });
  vm.runInContext(script, context, {timeout: 1000});
  return {
    get, pending, document, window,
    input: get('#prompt-tags-input'), menu: get('#tag-suggestions'),
    negative: get('#negative-prompt-tags-input'), negativeMenu: get('#negative-tag-suggestions'),
    async type(input, value, caret = value.length) {
      input.value = value; input.selectionStart = caret;
      await input.fire('input');
    },
    async timers() {
      const callbacks = [...timers.values()]; timers.clear();
      for (const callback of callbacks) callback();
      await settled();
    },
    async suggest(input, value, tags) {
      await this.type(input, value); await this.timers();
      pending.at(-1).respond(tags.map(tag => ({tag, count: 1})));
      await settled();
    },
  };
}
const scenarios = {
  async rapid_typing_coalesces_into_one_latest_request() {
    const h = setup();
    await h.type(h.input, 'c');
    await h.type(h.input, 'ca');
    await h.type(h.input, 'cat');
    assert.equal(h.pending.length, 0);
    await h.timers();
    assert.equal(h.pending.length, 1);
    assert.equal(h.pending[0].url, '/suggest-tags?q=cat');
    h.pending[0].respond([{tag: 'cat', count: 1}]); await settled();
    assert.deepEqual(h.menu.buttons.map(button => button.dataset.tag), ['cat']);
  },
  async keyboard_selection_and_escape() {
    const h = setup();
    await h.suggest(h.input, 'ca', ['cat', 'calico']);
    assert.equal(h.menu.hidden, false);
    await h.input.fire('keydown', {key: 'ArrowUp'});
    assert.equal(h.menu.buttons[1].active, true);
    await h.input.fire('keydown', {key: 'ArrowDown'});
    assert.equal(h.menu.buttons[0].active, true);
    const event = await h.input.fire('keydown', {key: 'Enter'});
    assert.equal(event.defaultPrevented, true);
    assert.equal(h.input.value, 'cat, ');
    assert.equal(h.menu.hidden, true);
    await h.suggest(h.input, 'ca', ['cat']);
    await h.input.fire('keydown', {key: 'Escape'});
    assert.equal(h.menu.hidden, true);
  },
  async replacing_token_preserves_surrounding_query_and_caret() {
    for (const separator of [',', '，', '、', ';', '；', '|', '｜', '\n']) {
      const h = setup(), value = `dog${separator} ca, bird`;
      await h.type(h.negative, value, value.indexOf('ca') + 2);
      await h.timers();
      assert.equal(h.pending.at(-1).url, '/suggest-tags?q=ca');
      h.pending.at(-1).respond([{tag: 'cat & "kitten"', count: 2}]);
      await settled();
      const button = h.negativeMenu.buttons[0];
      await h.negativeMenu.fire('click', {target: {closest: () => button}});
      assert.equal(h.negative.value, `dog${separator} cat & "kitten", bird`);
      assert.equal(h.negative.selectionStart, `dog${separator} cat & "kitten"`.length);
      assert.equal(h.input.value, '');
    }
  },
  async typing_invalidates_click_and_enter_during_debounce() {
    const h = setup();
    await h.suggest(h.input, 'ca', ['cat']);
    const stale = h.menu.buttons[0];
    await h.type(h.input, 'dog');
    assert.equal(h.menu.hidden, true);
    assert.equal(h.menu.buttons.length, 0);
    const event = await h.input.fire('keydown', {key: 'Enter'});
    assert.equal(event.defaultPrevented, undefined);
    await h.menu.fire('click', {target: {closest: () => stale}});
    assert.equal(h.input.value, 'dog');
    // Move the caret without typing: the menu still describes the old token.
    await h.type(h.input, 'ca, dog', 2); await h.timers();
    h.pending.at(-1).respond([{tag: 'cat', count: 1}]); await settled();
    const oldButton = h.menu.buttons[0];
    h.input.selectionStart = h.input.value.length;
    await h.input.fire('keydown', {key: 'Enter'});
    await h.menu.fire('click', {target: {closest: () => oldButton}});
    assert.equal(h.input.value, 'ca, dog');
    const down = h.input.fire('keydown', {key: 'ArrowDown'});
    await settled();
    assert.equal(h.pending.at(-1).url, '/suggest-tags?q=dog');
    h.pending.at(-1).respond([{tag: 'dog', count: 1}]);
    await down;
    await h.input.fire('keydown', {key: 'Enter'});
    assert.equal(h.input.value, 'ca, dog, ');
  },
  async old_fetch_completion_cannot_replace_new_results_or_other_input() {
    const h = setup();
    await h.type(h.input, 'ca'); await h.timers();
    const old = h.pending.at(-1);
    await h.suggest(h.input, 'dog', ['dog']);
    assert.equal(old.signal.aborted, true);
    old.respond([{tag: 'cat', count: 99}]); await settled();
    assert.deepEqual(h.menu.buttons.map(button => button.dataset.tag), ['dog']);
    await h.type(h.input, 'fox'); await h.timers();
    const positive = h.pending.at(-1);
    await h.suggest(h.negative, 'bl', ['blurry']);
    positive.respond([{tag: 'fox', count: 2}]); await settled();
    assert.equal(h.menu.hidden, true);
    assert.deepEqual(h.negativeMenu.buttons.map(button => button.dataset.tag), ['blurry']);
  },
  async old_json_completion_cannot_reopen_stale_results() {
    const h = setup();
    await h.type(h.input, 'ca'); await h.timers();
    const finish = h.pending.at(-1).respondDeferredJSON(); await settled();
    await h.suggest(h.input, 'dog', ['dog']);
    finish([{tag: 'cat', count: 99}]); await settled();
    assert.deepEqual(h.menu.buttons.map(button => button.dataset.tag), ['dog']);
  },
  async disabled_empty_and_failed_suggestions_leave_no_selectable_results() {
    const h = setup();
    await h.suggest(h.input, 'ca', ['cat']);
    const toggle = h.get('#autocomplete-enabled'); toggle.checked = false;
    await toggle.fire('change');
    assert.equal(h.menu.hidden, true);
    const count = h.pending.length;
    await h.type(h.input, 'dog'); await h.timers();
    assert.equal(h.pending.length, count);
    toggle.checked = true;
    await h.suggest(h.input, 'dog', []);
    assert.equal(h.menu.hidden, true);
    await h.type(h.input, 'x'); await h.timers();
    h.pending.at(-1).respond([], false); await settled();
    assert.equal(h.menu.hidden, true);
  },
};
(async () => {
  for (const [name, scenario] of Object.entries(scenarios)) {
    await scenario();
    console.log(`PASS ${name}`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
