
// Every menu of this shape — the account and Display both — dismisses the
// same way, so each gets the same three listeners.
Array.prototype.forEach.call(document.querySelectorAll('details.me'),
                             function (me) {
  function shut() { me.open = false; }
  // Escape closes it and puts the keyboard back on the control that opened
  // it. Without the second half the next Tab starts from the top of the
  // document, which on this page is the Back button.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !me.open) return;
    shut();
    var s = me.querySelector('summary');
    if (s && s.focus) s.focus();
  });
  // On the way *down*, not on the way up. Half the controls on the browse
  // page stop a click propagating — every filter chip does, because the
  // handler that opens a filter menu has to keep the handler that dismisses
  // one from undoing it — so a dismissal listening on the bubble never hears
  // about them. The account menu stayed open underneath a filter menu that
  // had just opened over it. Capture runs before any of them and before the
  // `<summary>` toggles, which is early enough that `me.open` still says
  // whether this click is the one opening it.
  document.addEventListener('click', function (e) {
    if (me.open && !me.contains(e.target)) shut();
  }, true);
  // Tabbing off the last row closes it too. Guarded on there being somewhere
  // the focus went: a click on the dead space inside the panel reports a
  // `relatedTarget` of null, and closing on that would make the menu
  // impossible to click around in.
  me.addEventListener('focusout', function (e) {
    if (me.open && e.relatedTarget && !me.contains(e.relatedTarget)) shut();
  });
});
