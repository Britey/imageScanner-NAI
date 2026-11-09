/* Paste into the benchmark Web UI's DevTools console (or browser evaluate API).
   No dependencies. Observes actual navigation/resources and drives normal input
   and scrolling; does not replace application fetch, SQL, rendering, or handlers. */
(() => {
  if (window.imgdupePerf?.observer) window.imgdupePerf.observer.disconnect();
  const longTasks = [];
  let observer = null;
  if (PerformanceObserver.supportedEntryTypes.includes("longtask")) {
    observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if (longTasks.length < 500) longTasks.push({ startTime: entry.startTime, duration: entry.duration });
      }
    });
    observer.observe({ type: "longtask", buffered: true });
  }
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const resource = e => ({ name: e.name, initiatorType: e.initiatorType,
    startTime: e.startTime, duration: e.duration, responseStart: e.responseStart,
    responseEnd: e.responseEnd, transferSize: e.transferSize,
    decodedBodySize: e.decodedBodySize, responseStatus: e.responseStatus });
  const capture = () => {
    const nav = performance.getEntriesByType("navigation")[0];
    return {
      url: location.href, capturedAt: new Date().toISOString(), userAgent: navigator.userAgent,
      viewport: { width: innerWidth, height: innerHeight },
      navigation: nav ? nav.toJSON() : null,
      paint: performance.getEntriesByType("paint").map(x => ({ name: x.name, startTime: x.startTime })),
      resources: performance.getEntriesByType("resource").map(resource),
      images: [...document.images].map(x => ({ src: x.currentSrc || x.src,
        complete: x.complete, width: x.naturalWidth, height: x.naturalHeight, loading: x.loading })),
      longTasks: [...longTasks], longTaskSupported: observer !== null,
      domElements: document.querySelectorAll("*").length,
      notes: ["Times are browser performance milliseconds, relative to navigation time origin.",
        "Buffered long tasks include only entries retained by this browser; not a complete CPU profile.",
        "Navigation timing includes actual HTTP wait; resource timing includes browser scheduling."]
    };
  };
  const autocompleteBurst = async ({ text = "common", intervalMs = 80, timeoutMs = 30000 } = {}) => {
    const input = document.querySelector("#prompt-tags-input");
    const enabled = document.querySelector("#autocomplete-enabled");
    if (!input || !enabled?.checked) throw new Error("Open the home page with autocomplete enabled first");
    const start = performance.now();
    input.value = "";
    for (const char of text) {
      input.value += char;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await delay(intervalMs);
    }
    let timedOut = false;
    while (!performance.getEntriesByType("resource").some(e => e.startTime >= start &&
      new URL(e.name).pathname === "/suggest-tags" && new URL(e.name).searchParams.get("q") === text)) {
      if (performance.now() - start > timeoutMs) { timedOut = true; break; }
      await delay(20);
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return { action: "autocompleteBurst", text, intervalMs, timedOut,
      elapsedMs: performance.now() - start,
      requests: performance.getEntriesByType("resource").filter(e => e.startTime >= start &&
        new URL(e.name).pathname === "/suggest-tags").map(resource),
      suggestionText: document.querySelector("#tag-suggestions")?.innerText,
      longTasks: longTasks.filter(e => e.startTime >= start) };
  };
  const scrollResults = async ({ timeoutMs = 30000 } = {}) => {
    const start = performance.now();
    const originalY = scrollY;
    const root = document.scrollingElement;
    for (let y = 0; y < root.scrollHeight; y += Math.max(100, innerHeight * .8)) {
      scrollTo(0, y);
      await delay(40);
      if (performance.now() - start > timeoutMs) break;
    }
    scrollTo(0, root.scrollHeight);
    while ([...document.images].some(image => !image.complete) && performance.now() - start < timeoutMs) {
      await delay(30);
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const result = { action: "scrollResults", elapsedMs: performance.now() - start,
      timedOut: [...document.images].some(x => !x.complete), totalImages: document.images.length,
      loadedImages: [...document.images].filter(x => x.complete && x.naturalWidth > 0).length,
      failedImages: [...document.images].filter(x => x.complete && x.naturalWidth === 0).length,
      resources: performance.getEntriesByType("resource").filter(x => new URL(x.name).pathname.startsWith("/thumb/")).map(resource),
      longTasks: longTasks.filter(e => e.startTime >= start) };
    scrollTo(0, originalY);
    return result;
  };
  window.imgdupePerf = { observer, capture, autocompleteBurst, scrollResults };
  return { installed: true, methods: ["capture()", "await autocompleteBurst()", "await scrollResults()"] };
})()
