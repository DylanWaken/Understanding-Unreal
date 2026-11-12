/* Preserve old chapter bookmarks after separating the independent essays. */
(() => {
  const chapter = location.hash.slice(1);
  const nanite = new Set(['pixels-and-triangles', 'clusters-and-boundaries', 'dag-and-cut', 'streaming-residency', 'gpu-visibility', 'visibility-rasterization', 'programmable-materials', 'shadow-views']);
  const lumen = new Set(['light-transport', 'tracing-representations', 'surface-cache', 'screen-probes', 'reflections-and-hit-lighting', 'time-and-invalidation', 'moving-and-large-worlds']);
  if (nanite.has(chapter)) location.replace('nanite.html#' + chapter);
  else if (lumen.has(chapter)) location.replace('lumen.html#' + chapter);
})();
