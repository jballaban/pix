
(function () {
  var head = document.getElementById('offhead');
  var say = document.getElementById('offsay');
  var go = document.getElementById('offgo');

  function show(title, text, retry) {
    head.textContent = title;
    say.textContent = text;
    go.hidden = !retry;
  }

  if (go) go.onclick = function () { location.reload(); };

  function look() {
    show('One moment', 'Finding out what happened.', false);
    // `no-store` because the answer to *can I reach home* must never come from
    // a cache that was filled at home.
    fetch('/healthz', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (state && state.index === false) {
          // Reachable, and telling us something specific: the app and the
          // projection it reads disagree about their shape. Naming that is the
          // difference between a five-second fix and an afternoon.
          show('The app needs updating',
               state.says || 'This app and the archive are out of step.',
               true);
          return;
        }
        // It answered and it is well, so whatever failed has stopped failing.
        show('It is back', 'The library is reachable again.', true);
      })
      .catch(function () {
        if (navigator.onLine === false) {
          show('No network',
               'This device is not on a network at all. The library is at '
               + 'home and will be here when you are back on one.',
               true);
        } else {
          // The distinction that matters on a phone: connected to something,
          // but not to home. Naming the two ways back is more use than any
          // description of the failure.
          show('Not at home',
               'This device is on a network, but the library cannot be '
               + 'reached from it. It lives on the NAS at home — connect to '
               + 'that network, or to the VPN, and it will be here.',
               true);
        }
      });
  }

  // A phone that rejoins a network should not need to be told to try again.
  window.addEventListener('online', look);
  look();
})();
