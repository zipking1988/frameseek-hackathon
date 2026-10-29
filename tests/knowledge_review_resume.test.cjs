'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function reviewPage(journey = false) {
  const cases = ['one', 'two'].map((id) => ({
    id, question: 'find box', source_kind: 'synthetic', source_id: 'video',
    source_fingerprint: 'a'.repeat(64), index_version: 'b'.repeat(64),
    scope_start: 0, scope_end: 8, categories: journey ? ['object', 'followup', 'seek'] : ['object'], sampled_frames: [`${id}-0`, `${id}-1`],
    frame_references: [{frame_id: `${id}-0`, t: 0}, {frame_id: `${id}-1`, t: 1}],
    frame_labels: [{frame_id: `${id}-0`, visible: null}, {frame_id: `${id}-1`, visible: null}],
    expected: null, predicted: null, visible_duration_seconds: null, reviewed: false, reviewer: '',
  }));
  const elements = [];
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.dataset = {}; this.value = ''; this.checked = false; elements.push(this); }
    append(...children) { this.children.push(...children); if (this.tag === 'select' && !this.value && children[0]) this.value = children[0].value; }
    setAttribute(key, value) { this[key] = value; }
    addEventListener(name, fn) { this.events[name] = fn; }
    click() {}
    close() {}
    showModal() {}
  }
  const ids = Object.fromEntries(['cases', 'reviewer', 'export', 'import', 'status', 'frame-dialog', 'frame-dialog-image', 'frame-dialog-title', 'frame-dialog-close'].map(id => [id, new Element('div')]));
  const document = {
    getElementById(id) { return ids[id]; },
    createElement(tag) { return new Element(tag); },
    createTextNode(text) { return { text }; },
    querySelector(selector) {
      let match = selector.match(/^input\[name="([^"]+)"\]\[value="([^"]+)"\]$/);
      if (match) return elements.find(e => e.name === match[1] && e.value === match[2]);
      match = selector.match(/^input\[name="([^"]+)"\]:checked$/);
      if (match) return elements.find(e => e.name === match[1] && e.checked);
      match = selector.match(/^input\[data-duration="([^"]+)"\]$/);
      if (match) return elements.find(e => e.dataset.duration === match[1]);
      throw new Error(`Unexpected selector: ${selector}`);
    },
  };
  let exported;
  class BlobCapture { constructor(parts) { exported = parts.join(''); } }
  const template = fs.readFileSync('scripts/knowledge_review_template.html', 'utf8');
  const script = template.match(/<script>([\s\S]*)<\/script>/)[1].replace('DATA_PLACEHOLDER', JSON.stringify(cases.map(c => ({case: c, images: ['x', 'y']}))));
  vm.runInNewContext(script, {document, CSS: {escape: s => s}, Blob: BlobCapture, URL: {createObjectURL: () => 'blob:fixture', revokeObjectURL() {}}, setTimeout() {}});
  const file = data => ({size: 100, text: async () => JSON.stringify(data)});
  return {cases, ids, elements, file, importFile: value => ids.import.events.change({target: {files: [value], value: 'selected'}}), exportData() { ids.export.events.click(); return JSON.parse(exported); }};
}

test('partial human review resumes without certifying unknown frames', async () => {
  const page = reviewPage();
  const records = page.cases.map(c => ({...c, reviewer: 'Reviewer', visible_duration_seconds: 3,
    frame_labels: [{frame_id: c.sampled_frames[0], visible: true}, {frame_id: c.sampled_frames[1], visible: null}]}));
  await page.importFile(page.file(records));
  assert.match(page.ids.status.textContent, /已恢复/);
  const exported = page.exportData();
  assert.equal(exported[0].reviewer, 'Reviewer');
  assert.deepEqual(exported[0].frame_labels.map(x => x.visible), [true, null]);
  assert.equal(exported[0].reviewed, false);
  assert.equal(exported[0].expected, null);
  assert.equal(exported[0].predicted, null);
});

test('wrong case or source rejects the whole import without overwriting labels', async () => {
  const page = reviewPage();
  const records = page.cases.map(c => ({...c, reviewer: 'Reviewer', visible_duration_seconds: 3,
    frame_labels: [{frame_id: c.sampled_frames[0], visible: true}, {frame_id: c.sampled_frames[1], visible: false}]}));
  await page.importFile(page.file(records));
  const before = page.exportData();
  const wrong = structuredClone(records);
  wrong[1].index_version = 'c'.repeat(64);
  wrong[0].frame_labels[0].visible = false;
  await page.importFile(page.file(wrong));
  assert.match(page.ids.status.textContent, /现有选择未改变/);
  assert.deepEqual(page.exportData(), before);
});

test('explicitly reviewed follow-up and seek details survive export and resume', async () => {
  const page = reviewPage(true);
  const records = page.cases.map(c => ({...c, reviewer: 'Reviewer', visible_duration_seconds: 3,
    frame_labels: [{frame_id: c.sampled_frames[0], visible: true}, {frame_id: c.sampled_frames[1], visible: null}],
    followup_check: {reviewed: true, parent_request_id: 'parent', resolved_question: 'find box', evidence_reference: 'trace', source_fingerprint: c.source_fingerprint, index_version: c.index_version},
    seek_checks: [{reviewed: true, frame_id: c.sampled_frames[0], target_time: 0, player_time: 0, evidence_reference: 'seek trace'}]}));
  await page.importFile(page.file(records));
  assert.match(page.ids.status.textContent, /已恢复/);
  const result = page.exportData();
  assert.deepEqual(result[0].followup_check, records[0].followup_check);
  assert.deepEqual(result[0].seek_checks, records[0].seek_checks);
  assert.equal(result[0].reviewed, false);
  const wrong = structuredClone(records);
  wrong[1].seek_checks[0].target_time = 99;
  await page.importFile(page.file(wrong));
  assert.match(page.ids.status.textContent, /现有选择未改变/);
  assert.deepEqual(page.exportData(), result);
});
