/* Runs first. One-time move of the browser settings saved under the old name (Latido):
   every "latido.*" key becomes "latidos.*" unless that key already exists. */
try {
  for (var i = localStorage.length - 1; i >= 0; i--) {
    var k = localStorage.key(i);
    if (k && k.indexOf("latido.") === 0) {
      var nk = "latidos." + k.slice(7);
      if (localStorage.getItem(nk) === null) localStorage.setItem(nk, localStorage.getItem(k));
      localStorage.removeItem(k);
    }
  }
} catch (e) {}
try { var t = localStorage.getItem("latidos.theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}
