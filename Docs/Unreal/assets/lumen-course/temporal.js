/* Scene-anchored observations. Frames are discrete captures, never interpolated. */
'use strict';
(() => {
  const manifests = new Map();
  const fetchManifest = url => {
    if (!manifests.has(url)) manifests.set(url, fetch(url).then(response => {
      if (!response.ok) throw new Error(`Manifest HTTP ${response.status}`);
      return response.json();
    }));
    return manifests.get(url);
  };
  const node = (tag, text, parent) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (parent) parent.append(element);
    return element;
  };
  async function initialize(host) {
    const manifestURL = new URL(host.dataset.manifest, document.baseURI);
    const layerID = host.dataset.layer;
    const panel = host.querySelector('.observation-controls');
    const image = host.querySelector('img');
    const caption = host.querySelector('.observation-caption');
    const status = host.querySelector('output');
    try {
      const manifest = await fetchManifest(manifestURL.href);
      const layer = manifest.layers.find(item => item.id === layerID);
      const experiments = manifest.experiments.map(experiment => ({...experiment,
        states: experiment.states.filter(state => state.layers[layerID])
      })).filter(experiment => experiment.states.length);
      if (!layer || !experiments.length) throw new Error('No captured observations for this layer');
      let experiment = experiments[0], index = 0, timer = null;
      const selectLabel = node('label', 'Experiment ', panel);
      const select = node('select', undefined, selectLabel);
      for (const item of experiments) {
        const option = node('option', item.label, select); option.value = item.id;
      }
      const previous = node('button', 'Previous', panel);
      const play = node('button', 'Play observations', panel);
      const next = node('button', 'Next', panel);
      for (const button of [previous, play, next]) button.type = 'button';
      const rangeLabel = node('label', 'Observation ', panel);
      const range = node('input', undefined, rangeLabel);
      range.type = 'range'; range.min = 0; range.step = 1;
      const facts = host.querySelector('.observation-facts');
      const legend = host.querySelector('.observation-legend');
      legend.textContent = layer.legend;
      const chart = node('div');
      chart.className = 'observation-chart';
      legend.after(chart);
      function stop() {
        if (timer !== null) window.clearInterval(timer);
        timer = null; play.textContent = 'Play observations'; play.setAttribute('aria-pressed', 'false');
      }
      function draw() {
        const state = experiment.states[index], observation = state.layers[layerID];
        range.max = experiment.states.length - 1; range.value = index;
        range.setAttribute('aria-valuetext', state.label);
        previous.disabled = index === 0; next.disabled = index === experiment.states.length - 1;
        play.disabled = experiment.states.length < 2;
        image.src = new URL(observation.image, manifestURL).href;
        image.alt = observation.alt;
        caption.textContent = observation.caption || layer.caption || '';
        status.textContent = `${state.label} · ${index + 1} of ${experiment.states.length} observations`;
        chart.replaceChildren();
        chart.hidden = !observation.chart;
        if (observation.chart) {
          node('p', observation.chart.title, chart);
          for (const item of observation.chart.values) {
            const row = node('label', `${item.label}: ${item.value} ${observation.chart.unit}`, chart);
            row.style.display = 'block';
            const meter = node('meter', undefined, row);
            meter.min = 0; meter.max = observation.chart.maximum; meter.value = item.value;
            meter.style.cssText = 'display:block;width:100%;height:1rem';
            meter.setAttribute('aria-label', `${item.label}: ${item.value} ${observation.chart.unit}; common scale 0 to ${observation.chart.maximum}`);
          }
        }
        facts.replaceChildren();
        for (const [name, value] of Object.entries({...state.facts, ...observation.facts})) {
          node('dt', name, facts); node('dd', String(value), facts);
        }
        const link = node('a', 'Decoded state and provenance');
        link.href = new URL(observation.record || state.record, manifestURL).href;
        const detail = host.querySelector('.observation-record'); detail.replaceChildren(link);
        detail.append(document.createTextNode(' · '));
        const fullImage = node('a', 'Full-size visualization', detail);
        fullImage.href = image.src;
        for (const download of observation.downloads || []) {
          detail.append(document.createTextNode(' · '));
          const extra = node('a', download.label, detail);
          extra.href = new URL(download.path, manifestURL).href;
        }
      }
      select.addEventListener('change', () => {
        stop(); experiment = experiments.find(item => item.id === select.value); index = 0; draw();
      });
      previous.addEventListener('click', () => { stop(); index = Math.max(0, index - 1); draw(); });
      next.addEventListener('click', () => { stop(); index = Math.min(experiment.states.length - 1, index + 1); draw(); });
      range.addEventListener('input', () => { stop(); index = Number(range.value); draw(); });
      play.addEventListener('click', () => {
        if (timer !== null) { stop(); return; }
        if (index === experiment.states.length - 1) index = 0;
        play.textContent = 'Pause'; play.setAttribute('aria-pressed', 'true'); draw();
        timer = window.setInterval(() => {
          index += 1; draw();
          if (index === experiment.states.length - 1) stop();
        }, 1500);
      });
      document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
      new IntersectionObserver(entries => {
        if (!entries[0].isIntersecting) stop();
      }).observe(host);
      draw();
    } catch (error) {
      status.textContent = 'The static observation remains available; interactive data could not be loaded.';
      console.error('Temporal observation', error);
    }
  }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target); initialize(entry.target);
    }
  }, {rootMargin: '500px'});
  document.querySelectorAll('.scene-observations').forEach(host => observer.observe(host));
})();
