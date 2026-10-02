
(function () {
  var KEY = 'pix2.install';
  function answered() {
    try { return localStorage.getItem(KEY) === 'no'; } catch (e) { return false; }
  }
  function remember() {
    try { localStorage.setItem(KEY, 'no'); } catch (e) {}
  }

  // Already installed: the offer would be absurd inside the thing it offers.
  // `display-mode` is the standard reading; `navigator.standalone` is how iOS
  // has always said it and still does.
  var inApp = (window.matchMedia
               && window.matchMedia('(display-mode: standalone)').matches)
              || navigator.standalone === true;
  if (inApp || answered()) return;

  var ua = navigator.userAgent || '';
  // iPadOS reports itself as a Mac, and has done for years; touch points are
  // what tell a tablet from a desktop that happens to have a trackpad.
  var ios = /iPhone|iPad|iPod/.test(ua)
            || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  var android = /Android/.test(ua);
  if (!ios && !android) return;

  var strip = null, prompter = null;

  function close() {
    remember();
    if (strip && strip.remove) strip.remove();
    strip = null;
  }

  function show() {
    if (strip) return;
    strip = document.createElement('div');
    strip.className = 'install';
    strip.id = 'install';

    var say = document.createElement('span');
    say.className = 'say';
    say.textContent = ios
      ? 'Add pix to your home screen: tap Share, then Add to Home Screen.'
      : (prompter ? 'Add pix to your home screen.'
                  : 'Add pix to your home screen from your browser menu.');
    strip.appendChild(say);

    if (prompter) {
      var go = document.createElement('button');
      go.className = 'primary go';
      go.textContent = 'Install';
      go.onclick = function () {
        // Whatever they answer, they have answered: a prompt dismissed at the
        // system level must not bring this bar back on the next page.
        var asked = prompter;
        prompter = null;
        close();
        if (asked && asked.prompt) asked.prompt();
      };
      strip.appendChild(go);
    }

    var no = document.createElement('button');
    no.className = 'no';
    no.textContent = 'No thanks';
    no.onclick = close;
    strip.appendChild(no);

    document.body.appendChild(strip);
  }

  // Chrome offers the prompt to the page instead of showing its own; taking it
  // is what turns the sentence into a button. It may never arrive — Firefox
  // and Samsung Internet do not send it — which is why the bar does not wait
  // for it.
  window.addEventListener('beforeinstallprompt', function (e) {
    if (e.preventDefault) e.preventDefault();
    prompter = e;
    if (strip && strip.remove) { strip.remove(); strip = null; }
    show();
  });
  window.addEventListener('appinstalled', close);
  show();
})();
