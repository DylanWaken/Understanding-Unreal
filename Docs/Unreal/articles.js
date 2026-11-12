/* Progressive reading aids. All article text and chapter links work without JS. */
(() => {
  'use strict';
  const root = document.documentElement;
  const contents = document.querySelector('.contents');
  const summary = contents.querySelector('summary');
  const chapters = [...document.querySelectorAll('.chapter')];
  const links = [...contents.querySelectorAll('a')];
  const position = document.querySelector('.reading-position');
  const progress = document.querySelector('.progress span');
  const theme = document.querySelector('.theme-button');
  const initialLabel = position.textContent;
  function applyTheme(value) {
    root.dataset.theme = value;
    theme.textContent = value === 'dark' ? 'Light' : 'Dark';
    theme.setAttribute('aria-label', `Switch to ${value === 'dark' ? 'light' : 'dark'} appearance`);
  }
  let preference = 'light';
  try { preference = localStorage.getItem('arda-essay-theme') || 'light'; } catch (_) { /* Storage can be disabled. */ }
  applyTheme(preference);
  theme.hidden = false;
  theme.addEventListener('click', () => {
    const value = root.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(value);
    try { localStorage.setItem('arda-essay-theme', value); } catch (_) { /* Reading still works. */ }
  });
  links.forEach(link => link.addEventListener('click', () => {
    contents.open = false;
    const target = document.getElementById(link.hash.slice(1));
    if (target) { target.tabIndex = -1; target.focus({preventScroll: true}); }
  }));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && contents.open) { contents.open = false; summary.focus(); }
  });
  document.addEventListener('click', event => {
    if (contents.open && !contents.contains(event.target)) contents.open = false;
  });
  let pending = false;
  function update() {
    pending = false;
    const y = scrollY + 160;
    let current = null;
    for (const chapter of chapters) if (chapter.offsetTop <= y) current = chapter;
    links.forEach(link => {
      if (current && link.hash === '#' + current.id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    position.textContent = current ? current.querySelector('h2').textContent.replace(/^\d+/, '').trim() : initialLabel;
    const article = document.querySelector('main');
    const fraction = Math.max(0, Math.min(1, (scrollY - article.offsetTop) / Math.max(1, article.offsetHeight - innerHeight)));
    progress.style.width = `${fraction * 100}%`;
  }
  function schedule() { if (!pending) { pending = true; requestAnimationFrame(update); } }
  addEventListener('scroll', schedule, {passive: true});
  addEventListener('resize', schedule);
  addEventListener('load', schedule);
  update();
  const definition = document.createElement('div');
  definition.id = 'term-definition'; definition.className = 'term-definition';
  definition.role = 'tooltip'; definition.hidden = true; document.body.append(definition);
  let described = null;
  function hideDefinition() {
    definition.hidden = true;
    if (described) described.removeAttribute('aria-describedby');
    described = null;
  }
  function showDefinition(link) {
    const term = window.ArdaGlossary && window.ArdaGlossary[link.dataset.term];
    if (!term) return;
    hideDefinition(); described = link;
    definition.textContent = term.definition; definition.hidden = false;
    link.setAttribute('aria-describedby', definition.id);
    const r = link.getBoundingClientRect();
    const box = definition.getBoundingClientRect();
    definition.style.left = `${Math.max(12, Math.min(r.left, innerWidth - box.width - 12))}px`;
    definition.style.top = `${r.bottom + box.height + 12 < innerHeight ? r.bottom + 8 : Math.max(8, r.top - box.height - 8)}px`;
  }
  document.querySelectorAll('a[data-term]').forEach(link => {
    link.addEventListener('pointerenter', () => showDefinition(link));
    link.addEventListener('focus', () => showDefinition(link));
    link.addEventListener('pointerleave', hideDefinition);
    link.addEventListener('blur', hideDefinition);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hideDefinition(); });
  addEventListener('scroll', () => {
    if (described && document.activeElement === described) {
      const bounds = described.getBoundingClientRect();
      if (bounds.bottom >= 0 && bounds.top <= innerHeight) showDefinition(described);
      else hideDefinition();
    } else hideDefinition();
  }, {passive:true});
})();
