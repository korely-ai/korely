// What render.mjs loads in the page: Excalidraw's own functions, bundled by
// esbuild, so the SVGs come out of the library itself (fonts included).
import React from "react";
import { createRoot } from "react-dom/client";
import {
  Excalidraw,
  convertToExcalidrawElements,
  exportToSvg,
  restore,
  serializeAsJSON,
} from "@excalidraw/excalidraw";

// The text is measured with the font the SVG will carry, so Excalifont has to
// be in document.fonts before convertToExcalidrawElements runs. Excalidraw
// registers its fonts (and their unicode ranges) when it mounts: mount one
// with every character the scenes use, and wait for those faces.
async function loadFonts(chars) {
  const div = document.createElement("div");
  div.style.cssText = "position:fixed;left:0;top:0;width:800px;height:600px";
  document.body.appendChild(div);
  const elements = convertToExcalidrawElements([
    { type: "text", x: 0, y: 0, text: chars, fontSize: 20, fontFamily: 5 },
  ]);
  createRoot(div).render(React.createElement(Excalidraw, { initialData: { elements } }));
  for (let i = 0; i < 300; i++) {
    await document.fonts.load("20px Excalifont", chars);
    if ([...document.fonts].some((f) => f.family.replace(/"/g, "") === "Excalifont" && f.status === "loaded")
        && document.fonts.check("20px Excalifont", chars)) {
      return;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error("Excalifont did not load");
}

// Skeletons (scenes.mjs) to elements, with seeds and ids that do not change
// from one run to the next, so a re-render of an unchanged scene is the same file.
function build(skeleton) {
  const elements = convertToExcalidrawElements(skeleton, { regenerateIds: false });
  const ids = new Map();
  elements.forEach((el, i) => {
    if (el.containerId && !skeleton.some((s) => s.id === el.id)) {
      ids.set(el.id, `${el.containerId}-label`);
    } else if (!skeleton.some((s) => s.id === el.id)) {
      ids.set(el.id, `el-${i}`);
    }
  });
  const re = (id) => ids.get(id) || id;
  return elements.map((el, i) => ({
    ...el,
    id: re(el.id),
    containerId: el.containerId ? re(el.containerId) : el.containerId,
    boundElements: el.boundElements ? el.boundElements.map((b) => ({ ...b, id: re(b.id) })) : el.boundElements,
    seed: 1 + ((i + 1) * 7919) % 2147483646,
    versionNonce: i + 1,
    version: 1,
    updated: 1,
    index: el.index,
  }));
}

function toFile(elements) {
  // `source` would be the renderer's page address: name the script instead.
  const file = JSON.parse(serializeAsJSON(elements, { viewBackgroundColor: "#ffffff", gridSize: null }, {}, "local"));
  file.source = "korely-agent scripts/make_diagrams.sh";
  return JSON.stringify(file, null, 2);
}

async function render(json, { padding, title }) {
  const data = restore(JSON.parse(json), null, null);
  const svg = await exportToSvg({
    elements: data.elements,
    appState: { ...data.appState, exportBackground: true, viewBackgroundColor: "#ffffff",
                exportWithDarkMode: false, exportEmbedScene: false },
    files: data.files || {},
    exportPadding: padding,
  });
  svg.setAttribute("role", "img");
  const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
  t.textContent = title;
  svg.insertBefore(t, svg.firstChild);
  // The page's background as a card with round corners: on GitHub's dark
  // theme the diagram reads as a white card, not a white hole.
  const bg = svg.querySelector(":scope > rect");
  if (bg && bg.getAttribute("fill") === "#ffffff") {
    bg.setAttribute("rx", "18");
    bg.setAttribute("ry", "18");
  }
  return svg.outerHTML;
}

window.K = { loadFonts, build, toFile, render };
window.K_READY = true;
