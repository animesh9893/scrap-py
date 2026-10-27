/* Adsterra display units. Pages mark where a banner may go with <div class="ad-slot" data-ad="160x600"></div>;
   ads.css decides on which screens each slot is shown, and a slot only loads once it is shown and near the viewport,
   so hidden units cost the visitor nothing.
   Adsterra's iframe banners read a global `atOptions`, so two on one page would overwrite each other's settings.
   Each banner therefore runs in its own small iframe with its own window and its own atOptions. */
(() => {
  const BANNERS = {
    '160x300': 'ce9fdd42b2bb83382b593a4e3b30395a',
    '160x600': '126b0e7deaa7df56781d99467bc63ef3',
    '468x60': '59efeed0441f8d5670957427de4deed9',
  };
  const NATIVE = '51293c44546e74bbda340ef98dc7c526';

  function bannerFrame(size) {
    const [width, height] = size.split('x').map(Number);
    const key = BANNERS[size];
    const frame = document.createElement('iframe');
    frame.width = width;
    frame.height = height;
    frame.title = 'Advertisement';
    frame.scrolling = 'no';
    frame.srcdoc = '<!doctype html><html><head><style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}</style></head><body>'
      + `<script>atOptions = { key: '${key}', format: 'iframe', height: ${height}, width: ${width}, params: {} };<\/script>`
      + `<script src="https://bauval.org/22/${key}"><\/script></body></html>`;
    return frame;
  }

  function fill(slot) {
    if (slot.dataset.loaded) return;
    slot.dataset.loaded = '1';
    if (slot.dataset.ad === 'native') {
      const box = document.createElement('div');
      box.id = `container-${NATIVE}`;
      const script = document.createElement('script');
      script.async = true;
      script.dataset.cfasync = 'false';
      script.src = `https://bauval.org/21/${NATIVE}`;
      slot.append(box, script);
    } else if (BANNERS[slot.dataset.ad]) {
      slot.append(bannerFrame(slot.dataset.ad));
    }
  }

  // A hidden slot (display: none) never intersects, so this also skips units the screen has no room for,
  // and loads them later if the window is resized to fit.
  const observer = 'IntersectionObserver' in window
    ? new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { fill(e.target); observer.unobserve(e.target); }
    }, { rootMargin: '300px 0px' })
    : null;

  function watch(slot) {
    if (slot.dataset.loaded || slot.dataset.watched) return;
    slot.dataset.watched = '1';
    if (observer) observer.observe(slot);
    else if (slot.offsetParent !== null) fill(slot);
  }

  function slot(size, tag = 'div') {
    const s = document.createElement(tag);
    s.className = size === 'native' ? 'ad-item ad-native' : `ad-item ad-slot ad-${size}`;
    s.dataset.ad = size;
    const label = document.createElement('p');
    label.className = 'ad-label';
    label.textContent = 'Advertisement';
    s.append(label);
    return s;
  }

  // The job list is rebuilt whenever the filters change. The native unit is created once and moved back into place,
  // so its ad is kept instead of loading again, and Adsterra's single container id stays unique on the page.
  let native, strip;
  /** Puts the native banner after the 6th job and a 468x60 strip after the 18th, at most once each. */
  function placeInList(list) {
    const jobs = [...list.children].filter((li) => !li.classList.contains('ad-item') && !li.classList.contains('empty'));
    if (jobs.length >= 6 && !(native && native.parentNode === list)) {
      native ??= slot('native', 'li');
      jobs[5].after(native);
      watch(native);
    }
    if (jobs.length >= 18 && !(strip && strip.parentNode === list)) {
      strip ??= slot('468x60', 'li');
      jobs[17].after(strip);
      watch(strip);
    }
  }

  function init() {
    document.querySelectorAll('.ad-slot[data-ad]').forEach(watch);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.KrawlrAds = { placeInList };
})();
