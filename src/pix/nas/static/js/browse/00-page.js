// What the server says about this page and this view (`pages.PageConfig`):
// one object, unpacked here so the rest of the script reads each by name.
const {VIEW, CHIPS, FIXED, FIXED_GROUPS={}, EXTRA, ADMIN, USERS, GROUPS,
       MARKS={}, UNREVIEWED, ARCHIVED, ARCHIVED_LABEL, EVENT_SEP, NO_EVENT,
       USUAL, PAGE, STACK, BACK, TIERS, GRID_GROUPS, ONE_FIELD,
       GROUPING} = window.PIX;

const grid=document.getElementById('grid');
const menu=document.getElementById('menu');
const chips=document.getElementById('chips');
const actions=document.getElementById('actions');
// Absent entirely for a non-admin: the page must not reach for controls
// the server would refuse anyway.
const selcount=document.getElementById('selcount');
const countEl=document.getElementById('count');
const binEl=document.getElementById('bincount');
// Every copy of the size control, not the first: the bar carries one where
// there is room and the account menu carries one where there is not, both are
// always rendered, and turning a phone over changes which is on screen without
// reloading the page. A `getElementById` here wired one of the two and left
// the other inert — a control that silently does nothing at one width.
const sizeOpts=Array.prototype.slice.call(
  document.querySelectorAll('.sizeopt'));
const note=document.getElementById('note');
const viewer=document.getElementById('viewer');
const vimg=document.getElementById('vimg'), vvid=document.getElementById('vvid');
const vmeta=document.getElementById('vmeta');
const stage=document.querySelector('.stage');
// Bounded so each request stays short: the server accepts 500, but a chunk that
// takes ten seconds gives no progress reading and holds the single worker.
const CHUNK=100;

