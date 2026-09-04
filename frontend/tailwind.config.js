/** Tailwind config for FinCal frontend (Issue #42: no runtime CDN).
 *
 *  Replaces the runtime Play-CDN compiler with a one-shot CLI build. Every
 *  utility class used by the SPA template / setup script is emitted into a
 *  committed `app/static/assets/tailwind.css`, so the site renders even when
 *  `unpkg.com` / `cdn.tailwindcss.com` are unreachable.
 *
 *  Content paths are relative to the repo root (the build script cds there).
 */
module.exports = {
  content: [
    "./app/static/index.html",
    "./app/static/assets/app-setup.js",
  ],
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        zinc: {
          850: "#1f1f23",
          950: "#09090b",
        },
      },
    },
  },
  plugins: [],
};
