
(function () {
  var bar = document.querySelector('.topbar');
  if (!bar) return;
  function say() {
    document.documentElement.style.setProperty(
      '--bar', Math.round(bar.getBoundingClientRect().height) + 'px');
  }
  say();
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(say).observe(bar);
  } else {
    window.addEventListener('resize', say);
  }
})();
